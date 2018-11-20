import boto3;
import sys, re, optparse
from datetime import datetime, date
import json
import requests
import socket
import logging
from logging.handlers import TimedRotatingFileHandler

assert sys.version_info < (3,0)

METRICS_TO_PUBLISH = {"memory" : "Total Yarn Memory",
                      "vCores" : "Total Virtual Cores"}
LOG_PATH = "/var/log/publish_capacity_scheduler_stats_to_cloudwatch.log"
NAMESPACE = "EMR/YarnUsage"

#tracking seen users so that we can publish data when we dont see them
listOfUsers = {}
listOfQueues = {}

#Create our logger
handler = TimedRotatingFileHandler(LOG_PATH, when='D', interval=1, backupCount=5, utc=True)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger = logging.getLogger("Rotating Log")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

#Create and configure clients.
if (len(sys.argv) != 2):
    logger.error("Unexpected number of arguments: %i. Expected 1.")
    exit(-1)

region=sys.argv[1]
logger.info("Region = %s", region)
cloudwatch = boto3.client('cloudwatch', region_name=region)
metric_data_to_publish = []

# Functions to get Cluster ID and whether this is a mastser node or not.
def get_cluster_id():
    with open ("/mnt/var/lib/info/job-flow.json", "r") as myfile:
        data=myfile.read()
    return json.loads(data, strict = False)["jobFlowId"]

def run_if_master_node():
    with open ("/mnt/var/lib/info/instance.json", "r") as myfile:
        data=myfile.read()
    is_master = json.loads(data, strict = False)["isMaster"]
    if is_master != True:
        logger.info("This is being run on a slave machine. Stopping script.")
        sys.exit(-1);

#Get Jobflow ID
jobflowId = get_cluster_id()

#Does the actual pushing of metrics to Cloudwatch.
def push_cloudwatch_metrics():
    if len(metric_data_to_publish) == 0:
        return

    cloudwatch.put_metric_data(
        Namespace = NAMESPACE,
        MetricData = metric_data_to_publish
    )
    logger.info("Publishing " + str(len(metric_data_to_publish)))
    logger.info(metric_data_to_publish)
    logger.info("================================================")
    #clear buffer
    del metric_data_to_publish[:]

#Stores metrics in a buffer prior to Cloudwatch. It will will submit metrics when it reaches 20 items.
def publish_cloudwatch_metric(metric_name, metric_value, dimensions):
    metric_data = {
        "MetricName" : metric_name,
        "Dimensions" : dimensions,
        'Timestamp': datetime.utcnow(),
        'Value': metric_value,
        'Unit': 'Count',
        'StorageResolution': 60
    }
    metric_data_to_publish.append(metric_data)
    #Publish metrics if we reach 20 items
    if len(metric_data_to_publish) >= 20:
        push_cloudwatch_metrics()

    #Reset seen users and queues
    for key in listOfUsers.keys():
        listOfUsers[key] = False

    for key in listOfQueues.keys():
        listOfUsers[key] = False

#Parse Response from Scheduler Output about Users.
def parse_user_info(user):
    if "resourcesUsed" in user:
        listOfUsers[user["username"]] = True
        resources_used = user["resourcesUsed"]
        for metric, metric_desc in METRICS_TO_PUBLISH.iteritems():
            if metric in resources_used:
                publish_cloudwatch_metric(metric_desc, resources_used[metric],
                                          [ { "Name" : "User", "Value" : user["username"] },
                                            { "Name" : "Jobflow Id", "Value" : jobflowId } ]
                                          )
                publish_cloudwatch_metric(metric_desc, resources_used[metric],
                                          [ { "Name" : "User", "Value" : user["username"] },
                                            { "Name" : "Jobflow Id", "Value" : "All Clusters" } ]
                                          )
            else:
                logger.info("ERROR: Failed to find " + metric_desc + " for User: " + user["username"])
    #Publish an empty metric for users if we dont see them active on this cluster.
    for key in listOfUsers.keys():
        if not listOfUsers[key]:
            for metric, metric_desc in METRICS_TO_PUBLISH.iteritems():
                publish_cloudwatch_metric(metric_desc, "0.0",
                                          [ { "Name" : "User", "Value" : key },
                                            { "Name" : "Jobflow Id", "Value" : jobflowId } ]
                                          )
                publish_cloudwatch_metric(metric_desc, "0.0",
                                          [ { "Name" : "User", "Value" : key },
                                            { "Name" : "Jobflow Id", "Value" : "All Clusters" } ]
                                          )

#Parse Response from Scheduler Output about Queues and Users. Called recursively to get the full path from root to leaf queues.
def parse_queue_info(queues_info, queue_full_name):
    for queue in queues_info["queue"]:
        if 'resourcesUsed' in queue:
            resources_used = queue["resourcesUsed"];
            current_queue_name = queue_full_name + "." + queue["queueName"]
            listOfQueues[current_queue_name] = True

            if 'type' in queue and queue["type"] == "capacitySchedulerLeafQueueInfo":
                for metric, metric_description in METRICS_TO_PUBLISH.iteritems():
                    if metric in resources_used:
                        publish_cloudwatch_metric(metric_description, resources_used[metric],
                                                  [ { "Name" : "Queue Name", "Value" : current_queue_name },
                                                    { "Name" : "Jobflow Id", "Value" : jobflowId }
                                                    ])
                        publish_cloudwatch_metric(metric_description, resources_used[metric],
                                                  [ { "Name" : "Queue Name", "Value" : current_queue_name },
                                                    { "Name" : "Jobflow Id", "Value" : "All Clusters" }
                                                    ])
                    else:
                        logger.info("ERROR: Failed to find " + metric_description + " for Queue: " + queue["queueName"])

        if 'users' in queue and queue["users"] is not None:
            for users in queue["users"]["user"]:
                parse_user_info(users)
        if 'queues' in queue:
            parse_queue_info(queue["queues"], current_queue_name)

    #Publish data for queues that may not be in use.
    for key in listOfQueues.keys():
        if not listOfQueues[key]:
            for metric, metric_desc in METRICS_TO_PUBLISH.iteritems():
                publish_cloudwatch_metric(metric_desc, "0.0",
                                          [ { "Name" : "Queue Name", "Value" : key },
                                            { "Name" : "Jobflow Id", "Value" : jobflowId }
                                            ])
                publish_cloudwatch_metric(metric_desc, "0.0",
                                          [ { "Name" : "Queue Name", "Value" : key },
                                            { "Name" : "Jobflow Id", "Value" : "All Clusters" }
                                            ])

#Call RM for Scheduler Metrics for Queue and User usage metrics
def parse_scheduler_response():
    response = requests.get('http://' + socket.gethostname() + ':8088/ws/v1/cluster/scheduler')
    rm_json = json.loads(response.text)

    scheduler_info = rm_json["scheduler"]["schedulerInfo"];
    queues = scheduler_info["queues"]
    parse_queue_info(queues, "root")

#Call RM for Cluster Metrics for overall metrics about what resources are available on this cluster
def parse_cluster_response():
    response = requests.get('http://' + socket.gethostname() + ':8088/ws/v1/cluster/metrics')

    rm_json = json.loads(response.text)
    cluster_metrics = rm_json["clusterMetrics"]
    if "totalMB" in cluster_metrics:
        publish_cloudwatch_metric("Total Yarn Memory Available", cluster_metrics["totalMB"],
                                  [ { "Name" : "Jobflow Id", "Value" : jobflowId } ])
        publish_cloudwatch_metric("Total Yarn Memory Available", cluster_metrics["totalMB"],
                                  [ { "Name" : "Jobflow Id", "Value" : "All Clusters" } ])
    else:
        logger.info("ERROR: Unable to find Total MB Metrics on cluster: " + jobflowId)

    if "totalVirtualCores" in cluster_metrics:
        publish_cloudwatch_metric("Total Virtual Cores Available", cluster_metrics["totalVirtualCores"],
                                  [ { "Name" : "Jobflow Id", "Value" : jobflowId } ])
        publish_cloudwatch_metric("Total Virtual Cores Available", cluster_metrics["totalVirtualCores"],
                                  [ { "Name" : "Jobflow Id", "Value" : "All Clusters" } ])
    else:
        logger.info("ERROR: Unable to find Total CPU Cores Metric on cluster: " + jobflowId)

#Main code for script.

run_if_master_node()

#Parse Scheduler Metrics (Queues, Users)
parse_scheduler_response();

#Parse Cluster metrics to get aggregates
parse_cluster_response();

#Push if there are any more metrics left in our buffer
push_cloudwatch_metrics()

