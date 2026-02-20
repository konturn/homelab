# Amperon

Noah's third OE job. Energy sector company.

**Work machine:** J3 (10.4.128.23, user konturn, macOS)
**Infrastructure:** Azure AKS cluster `prod-aks` in `prod-rg` (East US 2), 33 nodepools, ~73 active nodes, K8s 1.33.6
**Tooling:** az CLI works over SSH, kubectl needs interactive kubelogin (Azure AD) — can't automate pod-level checks non-interactively.

*Last synthesized: 2026-02-04*

## Facts
- Noah works at Amperon as part of OE strategy (one of 3 current jobs) (status, 2026-02-04)
- Amperon uses Azure AKS cluster named 'prod-aks' in resource group 'prod-rg' (East US 2 region) (status, 2026-02-04)
- prod-aks has 33 nodepools, ~73 active nodes, running Kubernetes 1.33.6 (status, 2026-02-04)
- J3 machine (10.4.128.23, user konturn) is the work machine for Amperon. macOS with homebrew, az CLI, kubectl with kubelogin (Azure AD auth) (status, 2026-02-04)
- kubectl requires interactive kubelogin Azure AD auth — cannot run non-interactively over SSH. az CLI commands work fine. (status, 2026-02-04)
