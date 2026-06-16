# InfiniteLoop Trading System

> An autonomous, self-improving futures day trading system built on three cooperative AI agents.

## What This Is

InfiniteLoop is a three-layer automated trading system designed to discover, execute, and manage futures trading strategies — starting with MES/ES and scaling over time to NQ, Gold, Oil, and crypto futures.

The system is built around a core loop: **find edge → trade it → grow it → find more edge**.

## The Three Layers

| Layer | Name | Where It Runs | Job |
|---|---|---|---|
| 1 | **Strategy Lab** | Local machine | Discover & validate strategies using Hermes AI |
| 2 | **Execution Agent** | Railway (cloud) | Trade the deployed strategy 24/7 |
| 3 | **Portfolio Manager** | Railway (cloud) | Scale capital, rotate strategies, expand instruments |

## Quick Links

- [Full Project Roadmap](./ROADMAP.md)
- [Architecture Deep Dive](./docs/ARCHITECTURE.md)
- [Strategy Lab](./strategy-lab/) *(code — Phase 1)*
- [Execution Agent](./execution-agent/) *(code — Phase 2)*
- [Portfolio Manager](./portfolio-manager/) *(code — Phase 4)*

## Current Status

🟡 **Pre-build** — Architecture designed, project initialized. Starting Phase 1.

## Core Philosophy

1. **Never risk what you can't afford to lose** — risk rules are hard limits, not suggestions
2. **One variable at a time** — the strategy loop changes one thing per iteration, always
3. **Live proof before scaling** — no new contracts, no new instruments without proven live performance
4. **The loop is the product** — the system improves itself; that's the edge
