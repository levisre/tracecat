import os

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.aws_certificatemanager import Certificate
from aws_cdk.aws_route53_targets import LoadBalancerTarget
from constructs import Construct

TRACECAT__APP_ENV = os.environ.get("TRACECAT__APP_ENV", "dev")
AWS_SECRET__ARN = os.environ["AWS_SECRET__ARN"]

AWS_ROUTE53__HOSTED_ZONE_ID = os.environ["AWS_ROUTE53__HOSTED_ZONE_ID"]
AWS_ROUTE53__HOSTED_ZONE_NAME = os.environ["AWS_ROUTE53__HOSTED_ZONE_NAME"]
AWS_ACM__CERTIFICATE_ARN = os.environ["AWS_ACM__CERTIFICATE_ARN"]
AWS_ACM__API_CERTIFICATE_ARN = os.environ["AWS_ACM__API_CERTIFICATE_ARN"]
AWS_ACM__RUNNER_CERTIFICATE_ARN = os.environ["AWS_ACM__RUNNER_CERTIFICATE_ARN"]


class TracecatEngineStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create cluster
        vpc = ec2.Vpc(self, "Vpc", vpc_name="tracecat-vpc")
        cluster = ecs.Cluster(
            self, "Cluster", cluster_name="tracecat-ecs-cluster", vpc=vpc
        )

        # Get hosted zone and certificate (created from AWS console)
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=AWS_ROUTE53__HOSTED_ZONE_ID,
            zone_name=AWS_ROUTE53__HOSTED_ZONE_NAME,
        )
        cert = Certificate.from_certificate_arn(
            self, "Certificate", AWS_ACM__CERTIFICATE_ARN
        )
        api_cert = Certificate.from_certificate_arn(
            self, "ApiCertificate", certificate_arn=AWS_ACM__API_CERTIFICATE_ARN
        )
        runner_cert = Certificate.from_certificate_arn(
            self, "RunnerCertificate", certificate_arn=AWS_ACM__RUNNER_CERTIFICATE_ARN
        )

        # Task execution IAM role (used across API and runner)
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name="TracecatFargateServiceExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        iam.Policy(
            self,
            "ExecutionRolePolicy",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/ecs/tracecat-*:*"
                    ],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=[AWS_SECRET__ARN],
                ),
            ],
            roles=[execution_role],
        )

        # Task role
        task_role = iam.Role(
            self,
            "TaskRole",
            role_name="TracecatTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        iam.Policy(
            self,
            "TaskRolePolicy",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "elasticfilesystem:ClientMount",
                        "elasticfilesystem:ClientWrite",
                        "elasticfilesystem:DescribeFileSystems",
                        "elasticfilesystem:DescribeMountTargets",
                        "elasticfilesystem:DescribeMountTargetSecurityGroups",
                    ],
                    resources=[
                        f"arn:aws:elasticfilesystem:{self.region}:{self.account}:file-system/{vpc.vpc_id}"
                    ],
                ),
            ],
            roles=[task_role],
        )

        # Set up a log group
        log_group = logs.LogGroup(
            self,
            "TracecatLogGroup",
            log_group_name="/ecs/tracecat",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Secrets
        tracecat_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "Secret", secret_complete_arn=AWS_SECRET__ARN
        )
        shared_secrets = {
            "TRACECAT__SIGNING_SECRET": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="signing-secret"
            ),
            "TRACECAT__SERVICE_KEY": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="service-key"
            ),
            "TRACECAT__DB_ENCRYPTION_KEY": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="db-encryption-key"
            ),
        }
        api_secrets = {
            **shared_secrets,
            "SUPABASE_JWT_SECRET": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="supabase-jwt-secret"
            ),
            "SUPABASE_PSQL_URL": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="supabase-psql-url"
            ),
            "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="openai-api-key"
            ),
        }
        runner_secrets = {
            **shared_secrets,
            "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                tracecat_secret, field="openai-api-key"
            ),
        }

        # # Define EFS
        # file_system = efs.FileSystem(
        #     self,
        #     "FileSystem",
        #     vpc=vpc,
        #     performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
        #     throughput_mode=efs.ThroughputMode.BURSTING,
        # )

        # Tracecat API Fargate Service
        api_task_definition = ecs.FargateTaskDefinition(
            self,
            "ApiTaskDefinition",
            execution_role=execution_role,
            task_role=task_role,
            # volumes=[
            #     ecs.Volume(
            #         name="Volume",
            #         efs_volume_configuration=ecs.EfsVolumeConfiguration(
            #             file_system_id=file_system.file_system_id
            #         ),
            #     )
            # ],
        )
        api_container = api_task_definition.add_container(  # noqa
            "ApiContainer",
            image=ecs.ContainerImage.from_asset(
                directory=".",
                file="Dockerfile",
                build_args={"API_MODULE": "tracecat.api.app:app"},
            ),
            cpu=256,
            memory_limit_mib=512,
            environment={
                "API_MODULE": "tracecat.api.app:app",
                "SUPABASE_JWT_ALGORITHM": "HS256",
            },
            secrets=api_secrets,
            port_mappings=[ecs.PortMapping(container_port=8000)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="tracecat-api", log_group=log_group
            ),
        )
        # api_container.add_mount_points(
        #     ecs.MountPoint(
        #         container_path="/home/apiuser/.tracecat",
        #         read_only=False,
        #         source_volume="Volume",
        #     )
        # )
        api_ecs_service = ecs.FargateService(
            self,
            "TracecatApiFargateService",
            cluster=cluster,
            task_definition=api_task_definition,
        )
        # API target group
        api_target_group = elbv2.ApplicationTargetGroup(
            self,
            "TracecatApiTargetGroup",
            target_type=elbv2.TargetType.IP,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            vpc=cluster.vpc,
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(120),
                timeout=Duration.seconds(10),
                healthy_threshold_count=5,
                unhealthy_threshold_count=2,
            ),
        )
        api_target_group.add_target(
            api_ecs_service.load_balancer_target(
                container_name="ApiContainer", container_port=8000
            )
        )

        # Tracecat Runner Fargate Service
        runner_task_definition = ecs.FargateTaskDefinition(
            self,
            "RunnerTaskDefinition",
            execution_role=execution_role,
            task_role=task_role,
            # volumes=[
            #     ecs.Volume(
            #         name="Volume",
            #         efs_volume_configuration=ecs.EfsVolumeConfiguration(
            #             file_system_id=file_system.file_system_id
            #         ),
            #     )
            # ],
        )
        runner_container = runner_task_definition.add_container(  # noqa
            "RunnerContainer",
            image=ecs.ContainerImage.from_asset(
                directory=".",
                file="Dockerfile",
                build_args={"API_MODULE": "tracecat.runner.app:app", "PORT": "8001"},
            ),
            cpu=256,
            memory_limit_mib=512,
            environment={"API_MODULE": "tracecat.runner.app:app", "PORT": "8001"},
            secrets=runner_secrets,
            port_mappings=[ecs.PortMapping(container_port=8001)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="tracecat-runner", log_group=log_group
            ),
        )
        # runner_container.add_mount_points(
        #     ecs.MountPoint(
        #         container_path="/home/apiuser/.tracecat",
        #         read_only=False,
        #         source_volume="Volume",
        #     )
        # )
        runner_ecs_service = ecs.FargateService(
            self,
            "TracecatRunnerFargateService",
            cluster=cluster,
            task_definition=runner_task_definition,
        )
        # Runner target group
        runner_target_group = elbv2.ApplicationTargetGroup(
            self,
            "TracecatRunnerTargetGroup",
            target_type=elbv2.TargetType.IP,
            port=8001,
            protocol=elbv2.ApplicationProtocol.HTTP,
            vpc=cluster.vpc,
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(120),
                timeout=Duration.seconds(10),
                healthy_threshold_count=5,
                unhealthy_threshold_count=2,
            ),
        )
        runner_target_group.add_target(
            runner_ecs_service.load_balancer_target(
                container_name="RunnerContainer", container_port=8001
            )
        )

        # Load balancer
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "TracecatEngineAlb",
            vpc=cluster.vpc,
            internet_facing=True,
            load_balancer_name="tracecat-engine-alb",
        )
        alb.add_listener(
            # Redirect HTTP to HTTPS
            "HttpListener",
            port=80,
            default_action=elbv2.ListenerAction.redirect(
                port="443",
                protocol="HTTPS",
                host="#{host}",
                path="/#{path}",
                query="#{query}",
                permanent=True,
            ),
        )

        # Main HTTPS listener
        listener = alb.add_listener("Listener", port=443, certificates=[cert])
        listener.add_action(
            "DefaultAction", action=elbv2.ListenerAction.fixed_response(status_code=404)
        )
        listener.add_action(
            "RootRedirect",
            priority=30,
            conditions=[elbv2.ListenerCondition.path_patterns(["/"])],
            action=elbv2.ListenerAction.redirect(
                host=f"api.{AWS_ROUTE53__HOSTED_ZONE_NAME}",  # Redirect to the API subdomain
                protocol="HTTPS",
                port="443",
                path="/",
                permanent=True,  # Permanent redirect
            ),
        )

        # Add subdomain listeners
        api_listener = alb.add_listener(
            "ApiHttpsListener",
            port=443,
            certificates=[api_cert],
            default_action=elbv2.ListenerAction.fixed_response(404),
        )
        api_listener.add_action(
            "ApiTarget",
            priority=10,
            conditions=[
                elbv2.ListenerCondition.host_headers(
                    [f"api.{AWS_ROUTE53__HOSTED_ZONE_NAME}"]
                )
            ],
            action=elbv2.ListenerAction.forward(target_groups=[api_target_group]),
        )

        runner_listener = alb.add_listener(
            "RunnerHttpsListener",
            port=443,
            certificates=[runner_cert],
            default_action=elbv2.ListenerAction.fixed_response(404),
        )
        runner_listener.add_action(
            "RunnerTarget",
            priority=20,
            conditions=[
                elbv2.ListenerCondition.host_headers(
                    [f"runner.{AWS_ROUTE53__HOSTED_ZONE_NAME}"]
                )
            ],
            action=elbv2.ListenerAction.forward(target_groups=[runner_target_group]),
        )

        # Create A record to point the hosted zone domain to the ALB
        route53.ARecord(
            self,
            "AliasRecord",
            record_name=AWS_ROUTE53__HOSTED_ZONE_NAME,
            target=route53.RecordTarget.from_alias(LoadBalancerTarget(alb)),
            zone=hosted_zone,
        )
        # Create A record for api.domain.com pointing to the ALB
        route53.ARecord(
            self,
            "ApiAliasRecord",
            record_name=f"api.{AWS_ROUTE53__HOSTED_ZONE_NAME}",
            target=route53.RecordTarget.from_alias(LoadBalancerTarget(alb)),
            zone=hosted_zone,
        )

        # Create A record for runner.domain.com pointing to the ALB
        route53.ARecord(
            self,
            "RunnerAliasRecord",
            record_name=f"runner.{AWS_ROUTE53__HOSTED_ZONE_NAME}",
            target=route53.RecordTarget.from_alias(LoadBalancerTarget(alb)),
            zone=hosted_zone,
        )

        # Add WAFv2 WebACL to the ALB

        # # Define the IP set for VPC's IP range
        # private_cidr_blocks = [subnet.ipv4_cidr_block for subnet in vpc.private_subnets]
        # vpc_ip_set = wafv2.CfnIPSet(
        #     self,
        #     "VpcIpSet",
        #     addresses=private_cidr_blocks,
        #     scope="REGIONAL",
        #     ip_address_version="IPV4",
        # )

        # web_acl = wafv2.CfnWebACL(
        #     self,
        #     "WebAcl",
        #     scope="REGIONAL",
        #     # Block ALL requests by default
        #     default_action=wafv2.CfnWebACL.DefaultActionProperty(block={}),
        #     visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        #         cloud_watch_metrics_enabled=True,
        #         metric_name="tracecatWebaclMetric",
        #         sampled_requests_enabled=True,
        #     ),
        #     rules=[
        #         # New rule for allowing health checks from within VPC
        #         wafv2.CfnWebACL.RuleProperty(
        #             name="AllowHealthChecks",
        #             priority=5,  # Set priority appropriately
        #             action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
        #             statement=wafv2.CfnWebACL.StatementProperty(
        #                 ip_set_reference_statement=wafv2.CfnWebACL.IPSetReferenceStatementProperty(
        #                     arn=vpc_ip_set.attr_arn
        #                 )
        #             ),
        #             visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        #                 cloud_watch_metrics_enabled=True,
        #                 metric_name="AllowHealthChecksMetric",
        #                 sampled_requests_enabled=True,
        #             ),
        #         ),
        #         # Block all traffic by default except for specific domain over HTTPS
        #         wafv2.CfnWebACL.RuleProperty(
        #             name="AllowSpecificDomainOverHttps",
        #             priority=10,
        #             action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
        #             statement=wafv2.CfnWebACL.StatementProperty(
        #                 and_statement=wafv2.CfnWebACL.AndStatementProperty(
        #                     statements=[
        #                         wafv2.CfnWebACL.StatementProperty(
        #                             byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
        #                                 search_string=os.environ.get(
        #                                     "TRACECAT__UI_SUBDOMAIN",
        #                                     "platform.tracecat.com",
        #                                 ),
        #                                 field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
        #                                     single_header={"name": "Host"}
        #                                 ),
        #                                 positional_constraint="EXACTLY",
        #                                 text_transformations=[
        #                                     wafv2.CfnWebACL.TextTransformationProperty(
        #                                         priority=0, type="LOWERCASE"
        #                                     )
        #                                 ],
        #                             )
        #                         ),
        #                         wafv2.CfnWebACL.StatementProperty(
        #                             byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
        #                                 search_string="https",
        #                                 field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
        #                                     single_header={"name": "X-Forwarded-Proto"}
        #                                 ),
        #                                 positional_constraint="EXACTLY",
        #                                 text_transformations=[
        #                                     wafv2.CfnWebACL.TextTransformationProperty(
        #                                         priority=0, type="NONE"
        #                                     )
        #                                 ],
        #                             )
        #                         ),
        #                         wafv2.CfnWebACL.StatementProperty(
        #                             or_statement=wafv2.CfnWebACL.OrStatementProperty(
        #                                 statements=[
        #                                     wafv2.CfnWebACL.StatementProperty(
        #                                         byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
        #                                             search_string="/api/",
        #                                             field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
        #                                                 single_query_argument={
        #                                                     "name": "uri"
        #                                                 }
        #                                             ),
        #                                             positional_constraint="STARTS_WITH",
        #                                             text_transformations=[
        #                                                 wafv2.CfnWebACL.TextTransformationProperty(
        #                                                     priority=0,
        #                                                     type="URL_DECODE",
        #                                                 )
        #                                             ],
        #                                         )
        #                                     ),
        #                                     wafv2.CfnWebACL.StatementProperty(
        #                                         byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
        #                                             search_string="/runner/",
        #                                             field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
        #                                                 single_query_argument={
        #                                                     "name": "uri"
        #                                                 }
        #                                             ),
        #                                             positional_constraint="STARTS_WITH",
        #                                             text_transformations=[
        #                                                 wafv2.CfnWebACL.TextTransformationProperty(
        #                                                     priority=0,
        #                                                     type="URL_DECODE",
        #                                                 )
        #                                             ],
        #                                         )
        #                                     ),
        #                                 ]
        #                             )
        #                         ),
        #                     ]
        #                 )
        #             ),
        #             visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
        #                 cloud_watch_metrics_enabled=True,
        #                 metric_name="allowSpecificDomainOverHttpsMetric",
        #                 sampled_requests_enabled=True,
        #             ),
        #         ),
        #     ],
        # )

        # # Associate the Web ACL with the ALB
        # wafv2.CfnWebACLAssociation(
        #     self,
        #     "WebAclAssociation",
        #     resource_arn=alb.load_balancer_arn,
        #     web_acl_arn=web_acl.attr_arn,
        # )