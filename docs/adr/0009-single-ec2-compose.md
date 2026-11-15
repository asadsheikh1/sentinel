# ADR-0009: Single Graviton EC2 with Podman Compose for the MVP

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Low-cost CPU hosting is required. The stack includes a memory-heavy inference process.

## Decision
One m7g.xlarge or r7g.xlarge instance runs the full stack with **rootless Podman** and `podman compose` behind Caddy, with no ALB, NAT, or RDS. Access via SSM, backups to S3, provisioned with OpenTofu.

## Consequences
About $135–180/month always-on, or less when scheduled. Single point of failure, which is acceptable for a portfolio MVP and documented with scaling paths.

## Alternatives considered
ECS Fargate (inference is a poor fit, plus ALB/NAT cost); EKS (cost and complexity); Lightsail (less IaC and IAM depth).
