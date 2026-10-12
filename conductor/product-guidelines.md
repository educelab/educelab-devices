# Product Guidelines

## Voice and tone

Precise, technical, evidence-first. Be explicit about uncertainty: hardware facts in `docs/hardware/` are marked **verified** (someone watched it happen), **assumed**, or **unknown**.

## Design principles

1. **Framework-free and blocking.** Devices never impose a threading or event model on consumers (ADR 0001).
2. **Fail loudly, never hang.** Every blocking call has a timeout. Out-of-range requests raise instead of being clamped. Stages verify where they arrived.
3. **Report what the hardware did, not what we wished.** No invented positions, no hidden decoding (ADR 0002), no state that can silently go stale, such as origins (ADR 0003).
4. **Write down what the hardware actually does.** A driver change isn't done until its `docs/hardware/` doc reflects what was learned.
5. **Minimal dependencies.** numpy is the only core dependency; vendor SDKs are lazy-imported optional extras.

## Vocabulary

`CONTEXT.md` is the glossary. Use its terms in code, docs, and commit messages, and avoid the terms it lists under _Avoid_.
