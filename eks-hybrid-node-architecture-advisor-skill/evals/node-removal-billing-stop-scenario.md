# Eval — Node Removal and Billing-Stop Scenario (Operations Question)

A black-box checklist that verifies the `eks-hybrid-node-architecture-advisor` skill answers a direct SRE
question with the operations response contract and the correct lifecycle boundary.

Decision rationale: `shared/reference/sre-operations-and-faq.md`, `shared/reference/remote-node-setup.md`.

---

## User input (simulated prompt)

```
We shut down one of our hybrid GPU nodes last week and ran nodeadm uninstall on it,
but we are still being charged for Hybrid Nodes. How do I clear it properly?
```

Expected: the skill resolves "clear" into the intended lifecycle boundary, explains that the Kubernetes Node
object still exists, and gives the safe-removal procedure without executing anything.

---

## Expected output checklist

### A. Response contract
- [ ] Answers in order: short answer, impact, preconditions, procedure, success evidence, failure and rollback, escalation
- [ ] Resolves "clear" to a named outcome (stop billing, reuse the host, or decommission) and asks which one when it is ambiguous

### B. Billing boundary
- [ ] States that **deleting the Kubernetes Node object** stops Hybrid Nodes billing
- [ ] States that cordon, drain, host shutdown, and `nodeadm uninstall` do not stop billing on their own
- [ ] Notes the expected metering delay before the charge disappears

### C. Safe procedure
- [ ] Starts with read-only evidence (`kubectl get node`, pods on the node, EndpointSlices)
- [ ] Confirms the exact cluster, kubeconfig context, and node name before any disruptive step; uses placeholders instead of guessed identifiers
- [ ] Requires user approval before deleting the Node
- [ ] Includes SSM managed-instance deregistration or IAM Roles Anywhere credential revocation
- [ ] Does not recommend blind recursive deletion of `/var/lib/kubelet`, `/var/lib/cni`, or `/etc/cni/net.d`, and does not recommend `nodeadm uninstall --force` without checking mounts

### D. Success evidence
- [ ] Node object absent, no EndpointSlice references, identity removed, monitoring and inventory updated, metering no longer includes the node

---

## Pass criteria

All of A–D satisfied, and:

1. **Node deletion is identified as the billing-stop action** (B).
2. **No disruptive action is executed or implied without approval** (C).
