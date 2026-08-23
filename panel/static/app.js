(() => {
  "use strict";

  const setMeter = (selector, value) => {
    const meter = document.querySelector(selector);
    if (meter) meter.value = Math.min(100, Math.max(0, Number(value) || 0));
  };

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        const original = button.textContent;
        button.textContent = "Copied";
        window.setTimeout(() => { button.textContent = original; }, 1400);
      } catch (_) {
        button.textContent = "Copy failed";
      }
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("form[data-busy]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.textContent = form.dataset.busy;
      }
    });
  });

  const updateStatus = async () => {
    if (!document.querySelector("[data-cpu]")) return;
    try {
      const response = await fetch("/api/status", {headers: {"Accept": "application/json"}});
      if (!response.ok) return;
      const data = await response.json();
      document.querySelectorAll("[data-status-text]").forEach((element) => { element.textContent = data.state; });
      document.querySelectorAll("[data-status-dot]").forEach((element) => {
        element.classList.toggle("online", data.active);
        element.classList.toggle("offline", !data.active);
      });
      const sentence = document.querySelector("[data-status-sentence]");
      if (sentence) sentence.textContent = data.active ? "ready for players" : "currently resting";
      const values = {
        "[data-cpu]": `${data.cpu}%`,
        "[data-memory]": `${data.memory}%`,
        "[data-disk]": `${data.disk}%`,
        "[data-memory-detail]": `${data.memory_used} of ${data.memory_total}`,
        "[data-disk-detail]": `${data.disk_free} free`,
        "[data-build]": data.build,
        "[data-world]": data.world,
        "[data-endpoint]": `${data.address}:${data.game_port}`,
      };
      Object.entries(values).forEach(([selector, value]) => {
        const element = document.querySelector(selector);
        if (element) element.textContent = value;
      });
      setMeter("[data-cpu-meter]", data.cpu);
      setMeter("[data-memory-meter]", data.memory);
      setMeter("[data-disk-meter]", data.disk);
    } catch (_) {
      // The next interval retries quietly; a stopped panel should not create UI noise.
    }
  };

  window.setInterval(updateStatus, 5000);
})();
