"""
Simple Python script using Jinja2 string replacement (primarily for docker compose or CFN template files)

The event variable contains a dict in the form:
{
  "CodePipeline.job": {
    "data": {
      "actionConfiguration": {
        "configuration": {
          "FunctionName": <invoked Lambda function,
          "UserParameters": {
            <string to look for>: <string to replace with>
          }
        }
      },
      "inputArtifacts": [
        {
          "location": {
            "type": "S3",
            "s3Location": {
              "objectKey": <object key of the input artifact>,
              "bucketName": <bucket of the input artifact>
            }
          },
          "name": <input artifact name>,
          "revision": "some string"
        }
      ],
      "outputArtifacts": [
        {
          "location": {
            "type": "S3",
            "s3Location": {
              "objectKey": <object key of the output artifact,
              "bucketName": <bucket of the output artifact>
            }
          },
          "name": <output artifact name>,
          "revision": "None"
        }
      ]
    },
    "id": <CodePipeline job id>,
    "accountId": <account id>
  }
}

CodePipeline passes artifacts between stages using S3. Input and output artifacts are always zip files, with the zip
archive names provided in the InputArtifacts and OutputArtifacts fields.

The "UserParameters" key contains a dict with the string to look for and the value to replace.

Note that Lambda only allows read/write to "/tmp" as workspace for the function, so all files have to go in there.

"""
import boto3
from jinja2 import Environment, FileSystemLoader
import zipfile
import ast
from os.path import basename
import logging
import json

# set logger object
FORMAT = "%(levelname)s"
logging.basicConfig(format=FORMAT)
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def put_job_success(job, message):
    """
    Notify CodePipeline of a successful job

    :param job: The CodePipeline job ID
    :param message: A message to be logged relating to the job status
    :return: none
    """
    code_pipeline = boto3.client("codepipeline")
    logger.info("Putting job success...{}".format(message))
    code_pipeline.put_job_success_result(jobId=job)


def put_job_failure(job, message):
    """
    Notify CodePipeline of a failed job

    :param job: The CodePipeline job ID
    :param message: A message to be logged relating to the job status
    :return: none
    """
    code_pipeline = boto3.client("codepipeline")
    logger.info("Putting job failure...{}".format(message))
    code_pipeline.put_job_failure_result(jobId=job, failureDetails={
                                                        "message": message,
                                                        "type": "JobFailed"
                                                    }
                                         )


def lambda_handler(event, context):
    # print the full event, mostly for troubleshooting
    logger.info("Event Details...\n{}".format(json.dumps(event, indent=4)))

    try:
        # list index 0 assumes only a single artifact location for now - should probably fix this.
        bucket_in = event["CodePipeline.job"]["data"]["inputArtifacts"][0]["location"]["s3Location"]["bucketName"]
        object_in = event["CodePipeline.job"]["data"]["inputArtifacts"][0]["location"]["s3Location"]["objectKey"]
        bucket_out = event["CodePipeline.job"]["data"]["outputArtifacts"][0]["location"]["s3Location"]["bucketName"]
        object_out = event["CodePipeline.job"]["data"]["outputArtifacts"][0]["location"]["s3Location"]["objectKey"]
        logger.info("InputArtifacts...{}/{}".format(bucket_in, object_in))
        logger.info("OutputArtifacts...{}/{}".format(bucket_out, object_out))

        # the boto3 s3 client object
        s3 = boto3.client("s3")
        # get the artifact file from S3
        logger.info("getting file from S3...")
        s3.download_file(bucket_in, object_in, "/tmp/artifact.zip")
        # unzip it
        zip = zipfile.ZipFile("/tmp/artifact.zip", 'r')
        # check each file in the zip
        logger.info("Files in the zip...{}".format(zip.namelist()))
        extracted_files = []
        for filename in zip.namelist():
            # only care about YAML files
            if "yaml" in filename.lower():
                logger.info("replacing values in file...{}".format(filename))
                zip.extract(filename, "/tmp")
                extracted_files.append(filename)
        zip.close()

        # build this by hand, for some reason using os.abspath() returns /var/task/whatever
        for filename in extracted_files:
            full_path = '/tmp/{}'.format(extracted_files[extracted_files.index(filename)])
            j2_env = Environment(loader=FileSystemLoader(full_path), trim_blocks=True)
            t = j2_env.get_template("")
            replacements = event["CodePipeline.job"]["data"]["actionConfiguration"]["configuration"]["UserParameters"]
            logger.info("Replacement values...\n{}".format(json.dumps(replacements, indent=4)))

            # change unicode string to dict. For some reason this is Unicode when running as a Lambda, but not when
            # running locally
            d = ast.literal_eval(replacements)
            output = t.render(d)
            logger.info("parsed template...\n{}".format(output))

            # prep output for writing back to S3
            parsed_template = open("/tmp/{}".format(filename), "w")
            parsed_template.write(output)
            parsed_template.close()

        # update the zip file
        zip = zipfile.ZipFile("/tmp/artifact.zip", 'w')
        for filename in extracted_files:
            logger.info("adding to archive...{}".format(filename))
            zip.write("/tmp/{}".format(filename), basename(filename)) # use basename to avoid adding the path in the archive
        logger.info("Files in the zip...{}".format(zip.namelist()))               # debug print
        zip.close()

        # write the zip file to the location specified in OutputArtifact
        with open("/tmp/artifact.zip", 'rb') as data:
            s3.put_object(Body=data,
                          Bucket=bucket_out,
                          Key=object_out,
                          ServerSideEncryption="aws:kms")

        # tell CodePipeline that we're done - should add a try-except clause to catch failures
        put_job_success(event["CodePipeline.job"]["id"], "variable replacement complete")

    except Exception as e:
        message = "Something failed in the variable replacement"
        logger.exception(message)
        put_job_failure(event["CodePipeline.job"]["id"], message)
        raise
