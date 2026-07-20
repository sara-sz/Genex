# Therapist environment — Terraform (DESIGN ONLY)

**Do not `init`, `plan`, or `apply` these files in this phase.** They describe
the intended Dev and Prod infrastructure so the founder can review the shape
before anything is provisioned. No real secret values appear here.

```
infra/terraform/
  modules/therapist_environment/   one reusable module for a full environment
  environments/therapist-dev/       dev instantiation (live later)
  environments/therapist-prod/      prod instantiation (DARK: min instances 0,
                                     authenticated invocation, empty Firestore)
```

## Key decisions encoded

- **Separate GCP + Firebase projects** per environment
  (`genex-provider-dev-2026`, `genex-provider-prod-2026`).
- **Cloud Run** `genex-api-therapist-dev` / `genex-api-therapist-prod`.
- **Service accounts** `therapist-api-dev-sa` / `therapist-api-prod-sa`.
- **Firestore (Native)** per project; Prod empty while dark.
- **Artifact Registry** `genex-therapist` per project — **Dev and Prod
  registries are separate**. Promotion copies an image **by digest** into the
  Prod registry and verifies the destination digest (correction #4); Prod holds
  no permanent access to Dev resources.
- **Secret Manager** references only (no values in code).
- **CORS** per environment (no wildcard; dev/prod origins never mixed).
- **Prod-dark**: `min_instance_count = 0`, `ingress` internal/authenticated,
  no seed, no debug, no public registration.
- **Budget alerts**, **logging**, **monitoring** on both.
```
