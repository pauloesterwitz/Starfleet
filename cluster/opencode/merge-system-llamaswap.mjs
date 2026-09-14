// merge-system-llamaswap.mjs — OpenCode plugin.
//
// Collapses the system prompt into ONE entry for the local `llamaswap` provider.
//
// WHY: opencode builds the system prompt as an array and sends each entry as its
// own `role: "system"` message. Qwen3.8-Flash-Next's chat template (served by
// SGLang) accepts exactly one system message and rejects any further one with
//   400  "System message must be at the beginning."
// It is NOT about ordering: verified directly against :28080 that TWO system
// messages, both at the very start and followed only by a user turn, still fail,
// while the identical request with one system message answers normally.
//
// The count is >1 on essentially every turn here because plugins legitimately
// append to that array — ponytail and i-have-adhd both call output.system.push().
// Joining is semantically neutral: the same text, same order, one message.
//
// Scoped to `llamaswap` so Anthropic and every other provider keep the
// multi-entry form they handle fine.
//
// MUST be listed LAST in opencode.json's `plugin` array: it has to run after
// every plugin that appends to the prompt.

export const MergeSystemForLlamaSwap = async () => ({
  'experimental.chat.system.transform': async (input, output) => {
    if (input?.model?.providerID !== 'llamaswap') return;
    if (!Array.isArray(output?.system) || output.system.length <= 1) return;
    const merged = output.system.join('\n\n');
    // splice, not reassignment: the other plugins mutate this array in place, so
    // opencode may still be holding the original reference.
    output.system.splice(0, output.system.length, merged);
  },
});

export default MergeSystemForLlamaSwap;
