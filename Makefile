# CDK commands for the worker cluster. Run from the repo root with your venv active.

CLUSTER_INFRA_DIR := worker-cluster/infra
CLUSTER_CDK := cd $(CLUSTER_INFRA_DIR) && cdk

.PHONY: cdk-synth cdk-bootstrap cdk-diff cdk-deploy cdk-destroy

cdk-synth:
	$(CLUSTER_CDK) synth

cdk-bootstrap:
	$(CLUSTER_CDK) bootstrap

cdk-diff:
	$(CLUSTER_CDK) diff

cdk-deploy:
	$(CLUSTER_CDK) deploy

cdk-destroy:
	$(CLUSTER_CDK) destroy
