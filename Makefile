# CDK commands for the worker cluster. Run from the repo root with your venv active and the AWS CDK CLI (`cdk`) installed.

CLUSTER_INFRA_DIR := worker-cluster/infra
CLUSTER_CDK := cd $(CLUSTER_INFRA_DIR) && cdk

.PHONY: cdk-synth cdk-bootstrap cdk-diff cdk-deploy cdk-destroy

cluster-synth:
	$(CLUSTER_CDK) synth

cluster-bootstrap:
	$(CLUSTER_CDK) bootstrap

cluster-diff:
	$(CLUSTER_CDK) diff

cluster-deploy:
	$(CLUSTER_CDK) deploy

cluster-destroy:
	$(CLUSTER_CDK) destroy
