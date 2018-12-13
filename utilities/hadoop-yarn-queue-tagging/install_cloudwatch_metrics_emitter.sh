#!/usr/bin/env bash

#Install Dependencies
sudo pip install boto3

sudo mkdir /usr/lib/publish_yarn_metrics/
chown hadoop:hadoop /usr/lib/publish_yarn_metrics/
wget -O /usr/lib/publish_yarn_metrics/publish_capacity_scheduler_stats_to_cloudwatch.py https://github.com/hocanint-amzn/aws-bigdata-samples/raw/master/utilities/hadoop-yarn-queue-tagging/publish_capacity_scheduler_stats_to_cloudwatch.py

REGION=`curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone | sed -r 's/.$//g'`
TMP_CRON_FILE_PATH=/tmp/tmp_cron_tab

#Add job to cron to run every 5 mins
crontab -l > $TMP_CRON_FILE_PATH
echo "*/5 * * * * python /usr/lib/publish_yarn_metrics/publish_capacity_scheduler_stats_to_cloudwatch.py $REGION >> /var/log/publish_capacity_scheduler_stats_to_cloudwatch.out 2>&1" >> $TMP_CRON_FILE_PATH
crontab $TMP_CRON_FILE_PATH
rm  $TMP_CRON_FILE_PATH
