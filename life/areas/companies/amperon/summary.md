# Amperon

Noah's third OE job. Energy sector company.

**Work machine:** J3 (10.4.128.23, user konturn, macOS)
**Infrastructure:** Azure AKS cluster `prod-aks` in `prod-rg` (East US 2), 33 nodepools, ~73 active nodes, K8s 1.33.6
**Tooling:** az CLI works over SSH, kubectl needs interactive kubelogin (Azure AD) — can't automate pod-level checks non-interactively.

*Last synthesized: 2026-02-04*
