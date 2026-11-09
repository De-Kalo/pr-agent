import json

import boto3
from botocore.exceptions import ClientError

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.secret_providers.secret_provider import SecretProvider


def _error_kind(error: Exception) -> str:
    # Keep the AWS error code for diagnostics; never log the associated message.
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        error_details = response.get("Error")
        if isinstance(error_details, dict):
            code = error_details.get("Code")
            if isinstance(code, str) and code:
                return code
    return type(error).__name__


class AWSSecretsManagerProvider(SecretProvider):
    _CANDIDATE_STAGE = "PR_AGENT_CANDIDATE"

    def __init__(self, *, settings=None, require_config_secret: bool = True):
        try:
            settings = get_settings() if settings is None else settings
            region_name = settings.get("aws_secrets_manager.region_name") or settings.get("aws.AWS_REGION_NAME")
            if region_name:
                self.client = boto3.client('secretsmanager', region_name=region_name)
            else:
                self.client = boto3.client('secretsmanager')

            self.secret_arn = settings.get("aws_secrets_manager.secret_arn")
            if require_config_secret and not self.secret_arn:
                raise ValueError("AWS Secrets Manager ARN is not configured")
        except Exception as e:
            # Avoid logging SDK error text, which can include credential-process output.
            get_logger().error(f"Failed to initialize AWS Secrets Manager Provider: {_error_kind(e)}")
            raise e

    def get_secret(self, secret_name: str) -> str:
        """
        Retrieve individual secret by name (for webhook tokens)
        """
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
            return response['SecretString']
        except Exception as e:
            # Omit the secret name because GitLab passes its webhook token here.
            get_logger().warning(f"Failed to get secret from AWS Secrets Manager: {_error_kind(e)}")
            if _error_kind(e) != "ResourceNotFoundException":
                raise
            return ""

    def get_secret_version(self, secret_name: str) -> tuple[str, str]:
        """Return the current value and version ID, or empty strings when the secret does not exist."""
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
        except Exception as e:
            get_logger().warning(f"Failed to get secret from AWS Secrets Manager: {_error_kind(e)}")
            if _error_kind(e) != "ResourceNotFoundException":
                raise
            return "", ""
        return response['SecretString'], response['VersionId']

    def replace_secret_if_current(self, secret_name: str, secret_value: str, current_version_id: str) -> bool:
        """
        Make secret_value current only if current_version_id is still current.
        Moving AWSCURRENT with RemoveFromVersionId is checked atomically by Secrets Manager.
        """
        version_id = self.client.put_secret_value(
            SecretId=secret_name, SecretString=secret_value, VersionStages=[self._CANDIDATE_STAGE]
        )['VersionId']
        try:
            self.client.update_secret_version_stage(
                SecretId=secret_name,
                VersionStage="AWSCURRENT",
                MoveToVersionId=version_id,
                RemoveFromVersionId=current_version_id,
            )
        except ClientError as e:
            if _error_kind(e) == "InvalidParameterException":
                return False
            get_logger().error(f"Failed to update secret in AWS Secrets Manager: {_error_kind(e)}")
            raise
        return True

    def get_all_secrets(self) -> dict:
        """
        Retrieve all secrets for configuration override
        """
        try:
            response = self.client.get_secret_value(SecretId=self.secret_arn)
            return json.loads(response['SecretString'])
        except Exception as e:
            get_logger().error(
                f"Failed to get secrets from AWS Secrets Manager {self.secret_arn}: {_error_kind(e)}"
            )
            return {}

    def store_secret(self, secret_name: str, secret_value: str):
        try:
            self.client.put_secret_value(
                SecretId=secret_name,
                SecretString=secret_value
            )
        except ClientError as e:
            if _error_kind(e) == "ResourceNotFoundException":
                try:
                    self.client.create_secret(
                        Name=secret_name,
                        SecretString=secret_value
                    )
                except ClientError as create_err:
                    # A concurrent first install created it; accept only an identical duplicate.
                    if (_error_kind(create_err) != "ResourceExistsException"
                            or self.get_secret(secret_name) != secret_value):
                        get_logger().error(
                            f"Failed to create secret in AWS Secrets Manager: {_error_kind(create_err)}"
                        )
                        raise
                return

            get_logger().error(f"Failed to store secret in AWS Secrets Manager: {_error_kind(e)}")
            raise e
        except Exception as e:
            get_logger().error(f"Failed to store secret in AWS Secrets Manager: {_error_kind(e)}")
            raise e
