# InfiniteLoop Phase 1 — Claude Code Kickoff Prompt

**Copy everything inside the code block below and paste it into Claude Code.**

---

```
Read CLAUDE.md completely. Then read PHASE1_BUILD_PLAN.md completely. Then read docs/ARCHITECTURE.md.

These three files are the complete specification for what we are building. Do not start writing code until you have read all three.

---

PROJECT SUMMARY (for quick orientation):

InfiniteLoop is a three-layer autonomous 0DTE options trading system for SPX.

- Layer 1 (what we are building now): Strategy Lab — discovers and validates a 0DTE spread strategy using Hermes 3 AI. Historical data comes from free sources: yfinance, CBOE VIX CSV, and py_vollib for synthetic options pricing.
- Layer 2 (Phase 2, later): Execution Agent on Railway — trades live via Webull API.
- Layer 3 (Phase 4, later): Portfolio Manager — scales and rotates strategies.

The instrument is SPX (SPXW) 0DTE vertical spreads and iron condors. Starting capital is $5,000. The broker is Webull. The AI optimizer is Hermes 3 via Ollama running locally.

---

YOUR RULES FOR THIS SESSION:

1. Work through PHASE1_BUILD_PLAN.md Section 2 step by step, in order.
2. Announce each step before starting: "--- Starting STEP X: [name] ---"
3. At every ⏸️ MANUAL STOP, stop completely. Tell me what I need to do, and wait for my confirmation before continuing. Do not skip manual stops.
4. After completing each coding step, run any verification commands in the prompt. Fix failures before proceeding.
5. If a step has tests, all tests must pass before moving to the next step.
6. If you need information from me (API key, confirmation, file location), ask clearly and wait.
7. If I tell you which step to resume from, go directly there — do not redo earlier steps.
8. Never change the hard-coded risk rules (forced_exit_hour, max_loss_pct, daily_halt_pct). If you find yourself wanting to, ask me first.

---

START HERE:

Begin with the ⏸️ MANUAL STOP 0 — Verify Prerequisites. Walk me through the checklist and wait for my confirmation before writing any code.
```

---

## How to use this file

1. Open VS Code in the `infiniteLoopProject` folder
2. Open the integrated terminal (`Ctrl+`` `)
3. Run: `claude`
4. Paste the block above when Claude Code starts
5. Follow the prompts — Claude Code will walk you through each step and pause where you need to act

## Resuming after an interruption

If this session is cut short, start a new Claude Code session and paste:

```
Read CLAUDE.md, PHASE1_BUILD_PLAN.md, and docs/ARCHITECTURE.md.

We are resuming Phase 1 of InfiniteLoop. We completed up to [STEP X — name].

Resume from [STEP X+1 — name]. All code from prior steps is already written — do not redo them. Follow the same rules as before: announce each step, pause at every ⏸️ MANUAL STOP, fix failures before moving on.
```

Fill in the step number where you left off.

---

## Manual Stops in this plan

| Stop | When | What Kirk does |
|------|------|----------------|
| ⏸️ STOP 0 | Before any code | Verify Python, Git, Ollama, .env, Railway DB |
| ⏸️ STOP 1 | Before STEP 1B | Download VIX_History.csv from CBOE manually (free, 30 seconds) |
| ⏸️ STOP 3 | Before STEP 12 | Confirm tests pass, Ollama running, data cached |
| ⏸️ STOP 4 | After STEP 14 | Review dashboard results, decide to save or keep iterating |
