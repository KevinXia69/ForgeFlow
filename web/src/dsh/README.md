# Vendored DeepSeek Harness client components

Source: https://github.com/deepseek-ai/deepseek-harness
Version: 0.2.1-rc.2
License: MIT (see LICENSE)

Ported source files:

- `packages/client/ui-chat/src/client/chat/ReasoningRow.tsx`
- `packages/client/ui-primitives/src/DisclosureRow.tsx`
- `packages/client/ui-primitives/src/TextShimmer.tsx`
- their CSS modules

Adapted only where ForgeFlow supplies its own React state/stream data. The
visual/interaction behavior stays the DSH component's.
