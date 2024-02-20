import streamlit as st 
import boto3   
import datetime
import pandas as pd

## Streamlit code
st.set_page_config(layout="wide", page_title="SageMaker Dashboard")
st.title("SageMaker Dashboard")
st.write("This is a dashboard to monitor SageMaker endpoints")

col1, col2 = st.columns(2)
with col1:
    idle_time = st.text_input(label = "Idle time (in secs)", value=3600)
with col2:
    st.write("")
    delete_button = st.button("Delete Idle Endpoints")

## SM Code
IDLE_TIME_IN_SECONDS = int(idle_time)
now = datetime.datetime.utcnow()
past = now - datetime.timedelta(seconds=IDLE_TIME_IN_SECONDS) 

sm_client = boto3.client('sagemaker')
cw_client = boto3.client('cloudwatch')

def is_serverless_endpoint(client, endpoint_name):
    endpoint = client.describe_endpoint(EndpointName = endpoint_name)
    endpoint_config = client.describe_endpoint_config(EndpointConfigName = endpoint["EndpointConfigName"])
    product_variants = endpoint_config["ProductionVariants"]
    return "ServerlessConfig" in product_variants[0]

def create_endpoint_table():
    endpoint_names = []
    ## List of end points
    endpoints =  sm_client.list_endpoints(
        SortBy = 'CreationTime',
        SortOrder = 'Descending',
        StatusEquals = 'InService',
    )["Endpoints"]

    for each in endpoints:
        name = each["EndpointName"]
        if is_serverless_endpoint(sm_client, name):
            continue
        
        response = cw_client.get_metric_statistics(
        Namespace='AWS/SageMaker',
        MetricName='Invocations',
        Dimensions=[
            {
                'Name': 'EndpointName',
                'Value': name
            },
            {
                'Name': 'VariantName',
                'Value': 'AllTraffic',
            },
        ],
        StartTime=past, 
        EndTime=now,
        Period=IDLE_TIME_IN_SECONDS,
        Statistics=['Sum']
        )
        
        idle_time = 0.0
        if len(response['Datapoints']) > 0:
            idle_time = response['Datapoints'][0]['Sum']
        
        #Create a dict with endpoint name and idle time
        endpoint_names.append({"EndpointName": name, "IdleTime": idle_time})
        
    return endpoint_names

## convert the dict to dataframe
endpoints = create_endpoint_table()
df = pd.DataFrame(endpoints)
st.table(df)

if delete_button:
    df = df[df["IdleTime"] == 0.0]
    for each in df["EndpointName"]:
        sm_client.delete_endpoint(EndpointName = each)
        st.write(f"Deleted endpoint {each}")
        st.experimental_rerun()