import os
import argparse
import math
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    set_seed,
    get_scheduler,
    SchedulerType
)
from datasets import load_from_disk
import torch
import torch.distributed as dist
from utils import create_dataloaders,get_module_class_from_name,save_model
import time
from tqdm import tqdm

from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    ShardingStrategy,
    BackwardPrefetch,
    CPUOffload,
)
from torch.distributed.fsdp.wrap import (
    transformer_auto_wrap_policy,
)
import functools
import apex



def parse_arge():
    """Parse the arguments."""
    parser = argparse.ArgumentParser()
    # add model id and dataset path argument
    parser.add_argument(
        "--model_id",
        type=str,
        default="google/flan-t5-xl",
        help="Model id to use for training.",
    )
    parser.add_argument("--dataset_path", type=str, default="lm_dataset", help="Path to dataset.")
    # add training hyperparameters for epochs, batch size, learning rate, and seed
    parser.add_argument("--epochs", type=int, default=1, help="Number of epochs to train for.")
    parser.add_argument("--max_steps", type=int, default=None, help="Number of epochs to train for.")
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=1,
        help="Batch size to use for training.",
    )
    parser.add_argument("--lr", type=float, default=3e-5, help="Learning rate to use for training.")
    parser.add_argument("--optimizer", type=str, default="adamw_hf", help="Learning rate to use for training.")
    parser.add_argument("--seed", type=int, default=42, help="Seed to use for training.")
    parser.add_argument("--access_token",type=str,default=None)
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")

    parser.add_argument(
        "--gradient_checkpointing",
        type=bool,
        default=True,
        help="Path to deepspeed config file.",
    )
    parser.add_argument(
        "--bf16",
        type=bool,
        default=True if torch.cuda.get_device_capability()[0] == 8 else False,
        help="Whether to use bf16.",
    )
    parser.add_argument("--fsdp", type=str, default=None, help="Whether to use fsdp.")
    parser.add_argument(
        "--fsdp_transformer_layer_cls_to_wrap",
        type=str,
        default=None,
        help="Which transformer layer to wrap with fsdp.",
    )
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--limit_all_gathers", type=bool, default=False)
    parser.add_argument("--forward_prefetch", type=bool, default=False)
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--model_dir",type=str,default="/opt/ml/model")

    args = parser.parse_known_args()
    return args


def training_function(args):
    # set seed
    set_seed(args.seed)

    from huggingface_hub.hf_api import HfFolder;
    HfFolder.save_token(args.access_token)

    dataset = load_from_disk(args.dataset_path)
    # load model from the hub
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        #token=args.access_token,
        cache_dir="/opt/ml/sagemaker/warmpoolcache",
        use_cache=False if args.gradient_checkpointing else True,  # this is needed for gradient checkpointing
    )


    model.gradient_checkpointing_enable()


    tokenizer = AutoTokenizer.from_pretrained(args.model_id)

    train_dataset = dataset["train"]
    eval_dataset = dataset["validation"]

    train_dataloader,eval_dataloader = create_dataloaders(train_dataset,eval_dataset,args.rank,args.world_size,args.seed,args.per_device_train_batch_size,args.per_device_train_batch_size)


    auto_wrap_policy = functools.partial(
        transformer_auto_wrap_policy,
        transformer_layer_cls={
            get_module_class_from_name(model,"GPTBigCodeBlock")
        },
    )

    torch.cuda.set_device(args.local_rank)
    
    dtype = torch.bfloat16

    mixed_precision_policy = MixedPrecision(param_dtype=dtype, reduce_dtype=dtype, buffer_dtype=dtype)

    model = FSDP(
        model,
        auto_wrap_policy=auto_wrap_policy,
        mixed_precision=mixed_precision_policy,
        sharding_strategy=ShardingStrategy.FULL_SHARD,  # SHARD_GRAD_OP #for Zero2 and FULL_SHARD for Zero3,
        cpu_offload=CPUOffload(offload_params=False),
        backward_prefetch=BackwardPrefetch.BACKWARD_PRE,  # BACKWARD_POST, BACKWARD_PRE
        forward_prefetch=args.forward_prefetch,
        limit_all_gathers=args.limit_all_gathers,
        device_id=torch.cuda.current_device(),
    )

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight", "layer_norm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ] 

    optimizer = apex.optimizers.FusedAdam(optimizer_grouped_parameters, lr=args.learning_rate)

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.rank==0:
        print(f"Number of update steps per epoch {num_update_steps_per_epoch}")
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
    )

    start = time.time()
    device = torch.device(f"cuda:{args.local_rank}")

    for epoch in range(args.num_train_epochs):

        model.train()
        total_steps=0
        fsdp_loss = torch.zeros(2).to(args.local_rank)

        for _, batch in enumerate(tqdm(train_dataloader,disable=not (args.rank==0))):

            batch = {k: v.to(device) for k, v in batch.items()}
            output = model(**batch)
            loss = output["loss"]
            loss.backward()
            fsdp_loss[0] += loss.item()
            fsdp_loss[1] += len(batch["input_ids"])
        
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            total_steps += 1
            if total_steps > args.max_steps:
                break
             

        torch.distributed.all_reduce(fsdp_loss, op=torch.distributed.ReduceOp.SUM)
        train_loss = fsdp_loss[0] / fsdp_loss[1]
        train_ppl = torch.exp(train_loss)

        if args.rank==0:
            print(f"******{epoch=}: {train_ppl=} {train_loss=}******")
        

        model.eval()
        eval_loss = 0
        fsdp_eval_loss = torch.zeros(2).to(args.local_rank)
        for _, batch in enumerate(tqdm(eval_dataloader,disable=not (args.rank==0))):
            batch = {k: v.to(device) for k, v in batch.items()}
            with torch.no_grad():
                outputs = model(**batch)
            loss = outputs["loss"]

            fsdp_eval_loss[0] += loss.item()
            fsdp_eval_loss[1] += len(batch["input_ids"])

        torch.distributed.all_reduce(fsdp_eval_loss, op=torch.distributed.ReduceOp.SUM)
        eval_loss = fsdp_eval_loss[0] / fsdp_eval_loss[1]
        eval_ppl = torch.exp(eval_loss)

        if args.rank==0:
            print(f"*******{epoch=}: {eval_ppl=} {eval_loss=}*******")
    
    save_model(model,tokenizer,args.model_dir,args.rank)
    if args.rank == 0:
        print("Training done!")
    dist.barrier()




def main():
    torch.distributed.init_process_group(
                "nccl"
            )
    args, _ = parse_arge()
    args.local_rank = int(os.environ["LOCAL_RANK"])
    args.rank = int(os.environ["RANK"])
    args.world_size = int(os.environ["WORLD_SIZE"])
    training_function(args)


if __name__ == "__main__":
    main()
