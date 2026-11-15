# ADR-0010: OpenTofu instead of Terraform

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Terraform moved to the Business Source Licence 1.1. The project prefers open-source tooling.

## Decision
Use OpenTofu (MPL-2.0), which is HCL-compatible.

## Consequences
Terraform skills transfer directly. The AWS provider is shared.

## Alternatives considered
Terraform (BSL); AWS CDK (Apache-2.0, viable, but HCL is more common in SRE roles); CloudFormation.
