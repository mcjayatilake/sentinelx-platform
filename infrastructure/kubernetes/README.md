# Kubernetes manifests

Kustomize-based manifests for deploying SentinelX to Kubernetes.

```
kubernetes/
├── base/                  Shared Deployments/Services/ConfigMap/Ingress
└── overlays/
    ├── dev/                 1 replica each, debug logging, :dev image tags
    ├── staging/              2 replicas each, :staging image tags
    └── production/            3+ replicas, :latest image tags, higher limits
```

PostgreSQL, Redis, and object storage are expected to be managed services
in staging/production (not deployed here) — point `sentinelx-secrets` at
them.

## Usage

```bash
# Render manifests without applying
kubectl kustomize infrastructure/kubernetes/overlays/dev

# Create the environment secret (never commit the real file)
cp infrastructure/kubernetes/base/secret.yaml.example /tmp/secret.yaml
# edit /tmp/secret.yaml with real values, then:
kubectl apply -f /tmp/secret.yaml -n sentinelx-dev

# Apply
kubectl apply -k infrastructure/kubernetes/overlays/dev
```

## Notes

- Container images (`ghcr.io/sentinelx/backend`, `ghcr.io/sentinelx/frontend`)
  are placeholders — point `images:` in each overlay's `kustomization.yaml`
  at your registry.
- The Ingress assumes an nginx ingress controller and cert-manager; adjust
  annotations/`ingressClassName` for your cluster.
- `beat` (Celery scheduler) is intentionally kept at a single replica in
  every overlay to avoid duplicate scheduled jobs.
