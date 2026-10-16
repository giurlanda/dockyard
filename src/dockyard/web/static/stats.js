// The CPU and RAM running Projects use, refreshed every few seconds: each element with
// data-stats fetches from that address and fills the data-stat elements inside it.
(() => {
  "use strict";
  const number = new Intl.NumberFormat(document.documentElement.lang, {
    maximumFractionDigits: 1,
  });
  const bytes = (n) =>
    n >= 1024 ** 3
      ? `${number.format(n / 1024 ** 3)} GiB`
      : `${number.format(Math.round(n / 1024 ** 2))} MiB`;

  for (const stats of document.querySelectorAll("[data-stats]")) {
    const show = (stat, text) => {
      for (const element of stats.querySelectorAll(`[data-stat="${stat}"]`)) {
        element.textContent = (element.dataset.template ?? "{value}").replace("{value}", text);
      }
    };

    const follow = async () => {
      try {
        const response = await fetch(stats.dataset.stats);
        if (response.ok) {
          const usage = await response.json();
          show("cpu", `${number.format(usage.cpu)}%`);
          show("memory", `${bytes(usage.memory)} / ${bytes(usage.memory_limit)}`);
          show("cpus", number.format(usage.cpus));
        } else {
          show("cpu", "–"); // not running any more: its status says so at the next reload
          show("memory", "–");
        }
      } catch {
        // Dockyard unreachable for a moment: keep trying
      }
      setTimeout(follow, 3000);
    };
    follow();
  }
})();
