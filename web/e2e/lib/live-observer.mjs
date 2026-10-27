/** Passed to addInitScript. Passive observation; never issues/retries a request. */
export function observeCaseStreams() {
  const original = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await original(...args);
    const input = args[0];
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href);
    if (url.origin === location.origin && /^\/api\/product\/cases\/[a-f0-9]{32}\/ask\/stream$/.test(url.pathname)) {
      response.clone().text().then(body => {
        const events = body.split(/\r?\n/).filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
        const final = events.findLast(event => event.type === "final");
        window.__forgeflowAcceptanceAnswer = { path: url.pathname, errors: events.filter(event => event.type === "error").length,
          final: final ? { answer: final.answer, mode: final.mode, source_mode: final.source_mode,
            case_context: final.case_context, fallback_reason: final.fallback_reason } : null };
      }).catch(() => { window.__forgeflowAcceptanceAnswer = { capture_failed: true }; });
    }
    return response;
  };
}
