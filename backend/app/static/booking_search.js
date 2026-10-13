/* Поиск брони на форме работы: набор только показывает список, выбор — явное действие. */
(function (root) {
  function normalizeQuery(q) {
    return String(q || "").trim().replace(/^#/, "");
  }

  function exactBooking(items, q) {
    var trimmed = normalizeQuery(q);
    if (!/^\d+$/.test(trimmed)) return null;
    var hit = null;
    var list = items || [];
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].id) === trimmed) {
        if (hit) return null;
        hit = list[i];
      }
    }
    return hit;
  }

  function bookingSearchOutcome(items, q, commit) {
    var list = items || [];
    if (commit) {
      var exact = exactBooking(list, q);
      if (exact) return { action: "select", item: exact, items: list, empty: false };
    }
    return { action: "list", item: null, items: list, empty: list.length === 0 };
  }

  function suggestStillCurrent(requestQ, currentQ, requestSeq, currentSeq) {
    return requestSeq === currentSeq && normalizeQuery(requestQ) === normalizeQuery(currentQ);
  }

  function bookingQueryKeyAction(key, highlightIndex, itemCount) {
    if (key === "ArrowDown" || key === "ArrowUp") return { type: "move", delta: key === "ArrowDown" ? 1 : -1 };
    if (key === "Enter") {
      if (highlightIndex >= 0 && highlightIndex < itemCount) return { type: "select-highlight" };
      return { type: "commit" };
    }
    return { type: "none" };
  }

  function nextHighlight(current, delta, count) {
    if (!count) return -1;
    if (current < 0) return delta > 0 ? 0 : count - 1;
    var n = count;
    return (current + delta + n) % n;
  }

  function applySuggestResponse(state, response) {
    var cur = state || {};
    if (!suggestStillCurrent(response.query, cur.query, response.seq, cur.seq)) {
      return {
        seq: cur.seq,
        query: cur.query,
        bookingId: cur.bookingId || "",
        items: cur.items || [],
        highlight: cur.highlight == null ? -1 : cur.highlight,
        selected: false,
        ignored: true
      };
    }
    var outcome = bookingSearchOutcome(response.items, response.query, !!response.commit);
    if (outcome.action === "select" && outcome.item) {
      return {
        seq: cur.seq,
        query: cur.query,
        bookingId: outcome.item.id,
        item: outcome.item,
        items: [],
        highlight: -1,
        selected: true,
        ignored: false
      };
    }
    return {
      seq: cur.seq,
      query: cur.query,
      bookingId: cur.bookingId || "",
      items: outcome.items,
      highlight: -1,
      selected: false,
      ignored: false,
      empty: outcome.empty
    };
  }

  var api = {
    normalizeQuery: normalizeQuery,
    exactBooking: exactBooking,
    bookingSearchOutcome: bookingSearchOutcome,
    suggestStillCurrent: suggestStillCurrent,
    bookingQueryKeyAction: bookingQueryKeyAction,
    nextHighlight: nextHighlight,
    applySuggestResponse: applySuggestResponse
  };
  root.lbBookingSearch = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
