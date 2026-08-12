# EKS demo workspace

## ⚠️ Read this before you run anything

This workspace bills **~$0.14/hour — about $103/month — from the moment the cluster
exists**, whether or not anything is deployed to it.

| Component | Cost | Stops when… |
|---|---|---|
| EKS control plane | ~$0.10/hr (~$73/mo) | the **cluster is deleted**. Scaling nodes to zero does nothing. |
| 2 × t3.small nodes | ~$0.042/hr (~$30/mo) | the node group is deleted |

A two-hour demo costs about **$1.40**. A forgotten cluster costs about **$103/month**.

**Set a calendar reminder for the same day, before you apply.**

## Why this exists

Everything this cluster does, ECS Fargate in `envs/dev` already does — for $0 at rest.
This workspace exists so the resume line "deployed to EKS" is true and screenshottable,
and so the deployment/service/ingress/HPA manifests are real rather than aspirational.

Being able to say *that* — "I ran it on Fargate because it scales to zero, and I can
demonstrate the Kubernetes path when it's warranted" — is a stronger answer than having
left a cluster running.

## Run the demo

```bash
# 0. Confirm what dev exposes (the agent role, so pods get the same identity)
terraform -chdir=../dev output agent_role_arn

# 1. Plan and apply, with your own IP for API access
terraform init
terraform plan -var agent_role_arn=<arn> -var 'allowed_api_cidrs=["<your-ip>/32"]'
terraform apply

# 2. Point kubectl at it
$(terraform output -raw kubeconfig_command)

# 3. Deploy
kubectl apply -k ../../../k8s/

# 4. Watch it come up
kubectl -n fraud-lake get pods,svc,hpa -w
```

## Capture for the README

1. `kubectl -n fraud-lake get pods,svc,ingress,hpa` — the whole stack in one output.
2. `kubectl -n fraud-lake describe hpa fraud-lake-api` showing current vs target CPU.
3. A load test driving the HPA to scale out:
   ```bash
   kubectl -n fraud-lake run load --rm -it --image=busybox --restart=Never -- \
     sh -c "while true; do wget -q -O- http://fraud-lake-api/health; done"
   ```
4. The EKS console showing the cluster and node group.

## ⚠️ Tear down — today

```bash
terraform destroy
```

Then confirm nothing survived:

```bash
aws eks list-clusters --region us-east-1 --profile fraud-lake
```

An empty list is the only acceptable answer. If the destroy fails partway — usually on a
load balancer created by an ingress that Terraform does not know about — delete the
Kubernetes services first (`kubectl delete -k ../../../k8s/`), then destroy again.

That failure mode is worth knowing about: an ingress-provisioned ALB is not in Terraform
state, so `terraform destroy` cannot remove it, and it keeps billing after the cluster is
gone.
