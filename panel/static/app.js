(() => {
  "use strict";

  const setMeter = (selector, value) => {
    const meter = document.querySelector(selector);
    if (meter) meter.value = Math.min(100, Math.max(0, Number(value) || 0));
  };

  const copyText = async (value) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const temporary = document.createElement("textarea");
    temporary.value = value;
    temporary.setAttribute("readonly", "");
    temporary.className = "copy-fallback";
    document.body.append(temporary);
    temporary.select();
    const copied = document.execCommand("copy");
    temporary.remove();
    if (!copied) throw new Error("Copy failed");
  };

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const original = button.textContent;
    try {
      await copyText(button.dataset.copy || "");
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = "Copy failed";
    }
    window.setTimeout(() => { button.textContent = original; }, 1400);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (form.dataset.busy) {
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.textContent = form.dataset.busy;
      }
    }
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

  const updatePublicStatus = async () => {
    if (!document.querySelector("[data-public-status]")) return;
    try {
      const response = await fetch("/api/public-status", {
        headers: {"Accept": "application/json"},
        cache: "no-store",
      });
      if (!response.ok) return;
      const data = await response.json();
      const serverName = document.querySelector("[data-public-server-name]");
      const state = document.querySelector("[data-public-state]");
      const count = document.querySelector("[data-public-player-count]");
      const dot = document.querySelector("[data-public-status-dot]");
      if (serverName) serverName.textContent = data.server_name;
      if (state) state.textContent = data.state;
      if (count) count.textContent = `${data.player_count} / ${data.max_players} Players`;
      if (dot) {
        dot.classList.toggle("online", data.active);
        dot.classList.toggle("offline", !data.active);
      }
      const list = document.querySelector("[data-public-player-list]");
      if (list) {
        list.replaceChildren();
        if (data.players.length) {
          data.players.forEach((name) => {
            const badge = document.createElement("b");
            badge.textContent = name;
            list.append(badge);
          });
        } else {
          const empty = document.createElement("em");
          empty.textContent = "No players online";
          list.append(empty);
        }
      }
    } catch (_) {
      // Keep the most recently rendered server snapshot until the next retry.
    }
  };

  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };

  const renderPlayers = (data) => {
    const list = document.querySelector("[data-player-list]");
    if (!list) return;
    list.querySelectorAll("[data-player-row]").forEach((row) => row.remove());
    const empty = list.querySelector("[data-player-empty]");
    if (empty) empty.classList.toggle("hidden", data.players.length > 0);

    data.players.forEach((player) => {
      const row = element("div", "table-row");
      row.dataset.playerRow = "";

      const identity = element("span");
      identity.append(element("strong", "", player.name));
      identity.append(element("small", "", `${player.connection_count} connection${player.connection_count === 1 ? "" : "s"}`));
      row.append(identity);

      const identifier = element("span");
      identifier.append(element("code", "", player.platform_id));
      row.append(identifier);

      const status = element("span");
      status.append(element("span", `status-label ${player.online ? "online" : "offline"}`, player.status));
      if (player.online) status.append(element("small", "", player.connected));
      row.append(status);

      row.append(element("span", "", player.last_connection));
      const access = element("span");
      access.append(element("span", `role-label ${player.role.toLowerCase()}`, player.role));
      row.append(access);

      const actions = element("span", "table-actions player-actions");
      const copyId = element("button", "text-button", "Copy ID");
      copyId.type = "button";
      copyId.dataset.copy = player.platform_id;
      actions.append(copyId);
      const copyKick = element("button", "text-button", "Copy kick command");
      copyKick.type = "button";
      copyKick.dataset.copy = `kick ${player.name}`;
      actions.append(copyKick);

      const banForm = document.createElement("form");
      banForm.method = "post";
      banForm.action = `/player/${encodeURIComponent(player.platform_id)}/ban`;
      banForm.dataset.confirm = `${player.banned ? "Remove the ban for" : "Ban"} ${player.name}?`;
      const csrf = document.createElement("input");
      csrf.type = "hidden";
      csrf.name = "csrf_token";
      csrf.value = list.dataset.csrf || "";
      banForm.append(csrf);
      const action = document.createElement("input");
      action.type = "hidden";
      action.name = "action";
      action.value = player.banned ? "unban" : "ban";
      banForm.append(action);
      const banButton = element("button", `text-button${player.banned ? "" : " danger"}`, player.banned ? "Unban" : "Ban");
      banButton.type = "submit";
      banForm.append(banButton);
      actions.append(banForm);
      row.append(actions);
      list.insertBefore(row, empty || null);
    });

    const count = document.querySelector("[data-player-count]");
    const known = document.querySelector("[data-known-player-count]");
    if (count) count.textContent = `${data.player_count} / ${data.max_players}`;
    if (known) known.textContent = data.players.length;
  };

  const updatePlayers = async () => {
    if (!document.querySelector("[data-player-list]")) return;
    try {
      const response = await fetch("/api/players", {headers: {"Accept": "application/json"}});
      if (!response.ok) return;
      renderPlayers(await response.json());
    } catch (_) {
      // The current table remains useful if a transient refresh fails.
    }
  };

  const setupModSearch = () => {
    const searchRoot = document.querySelector("[data-mod-search]");
    if (!searchRoot) return;
    const submitButton = searchRoot.querySelector("[data-mod-search-submit]");
    const queryInput = searchRoot.querySelector("input[name='search_query']");
    const status = searchRoot.querySelector("[data-mod-search-status]");
    const resultsTable = searchRoot.querySelector("[data-mod-search-results]");
    const providerSelect = document.querySelector("form[action$='/mods/install/provider'] select[name='provider']");
    if (!submitButton || !queryInput || !status || !resultsTable || !providerSelect) return;

    const clearResultRows = () => {
      resultsTable.querySelectorAll("[data-mod-search-row]").forEach((row) => row.remove());
    };

    const setStatus = (message, level = "") => {
      status.textContent = message;
      status.classList.remove("error", "success");
      if (level) status.classList.add(level);
    };

    const renderResults = (provider, query, results) => {
      clearResultRows();
      if (!results.length) {
        resultsTable.classList.add("hidden");
        setStatus(`No packages found for "${query}".`, "");
        return;
      }

      const csrfToken = searchRoot.dataset.csrf || "";
      results.forEach((item) => {
        const row = element("div", "table-row");
        row.dataset.modSearchRow = "";

        const packageCell = element("span");
        packageCell.append(element("strong", "", `${item.namespace}-${item.name}`));
        packageCell.append(element("small", "", item.description || "No description available."));
        row.append(packageCell);

        const latestCell = element("span");
        latestCell.append(element("strong", "", item.version || "latest"));
        latestCell.append(element("small", "", `${Number(item.downloads || 0).toLocaleString()} downloads`));
        row.append(latestCell);

        const signalsCell = element("span");
        signalsCell.append(element("strong", "", `Score ${Number(item.rating_score || 0)}`));
        signalsCell.append(element("small", "", item.installed ? "Already installed" : "Not installed"));
        row.append(signalsCell);

        const actionsCell = element("span", "table-actions");
        const installForm = document.createElement("form");
        installForm.method = "post";
        installForm.action = "/mods/install/provider";
        installForm.dataset.busy = "Downloading and installing mods...";
        [
          ["csrf_token", csrfToken],
          ["provider", provider],
          ["namespace", item.namespace],
          ["package_name", item.name],
          ["version", item.version || ""],
        ].forEach(([name, value]) => {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          input.value = value;
          installForm.append(input);
        });
        const installButton = element(
          "button",
          "text-button",
          item.installed ? "Reinstall latest" : "Install latest",
        );
        installButton.type = "submit";
        installForm.append(installButton);
        actionsCell.append(installForm);

        if (item.package_url) {
          const packageLink = document.createElement("a");
          packageLink.href = item.package_url;
          packageLink.target = "_blank";
          packageLink.rel = "noopener noreferrer";
          packageLink.textContent = "View";
          actionsCell.append(packageLink);
        }

        row.append(actionsCell);
        resultsTable.append(row);
      });

      resultsTable.classList.remove("hidden");
      setStatus(
        `Found ${results.length} package${results.length === 1 ? "" : "s"} on ${provider}.`,
        "success",
      );
    };

    const search = async () => {
      const query = queryInput.value.trim();
      if (query.length < 2) {
        clearResultRows();
        resultsTable.classList.add("hidden");
        setStatus("Enter at least 2 characters to search.", "error");
        return;
      }

      const provider = providerSelect.value;
      const providerLabel = providerSelect.options[providerSelect.selectedIndex]?.textContent || provider;
      const searchUrl = `/api/mods/search?provider=${encodeURIComponent(provider)}&q=${encodeURIComponent(query)}&limit=25`;
      const previousText = submitButton.textContent;
      submitButton.disabled = true;
      submitButton.textContent = "Searching...";
      setStatus("Searching provider catalog...", "");
      try {
        const response = await fetch(searchUrl, {
          headers: {"Accept": "application/json"},
          cache: "no-store",
        });
        let payload = {};
        try {
          payload = await response.json();
        } catch (_) {
          payload = {};
        }
        if (!response.ok) {
          const message = typeof payload.error === "string" ? payload.error : "Search failed.";
          throw new Error(message);
        }
        const results = Array.isArray(payload.results) ? payload.results : [];
        renderResults(provider, query, results);
        setStatus(
          `Found ${results.length} package${results.length === 1 ? "" : "s"} on ${providerLabel}.`,
          "success",
        );
      } catch (error) {
        clearResultRows();
        resultsTable.classList.add("hidden");
        setStatus(error instanceof Error ? error.message : "Search failed.", "error");
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = previousText;
      }
    };

    submitButton.addEventListener("click", search);
    queryInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      search();
    });
  };

  updateStatus();
  updatePublicStatus();
  updatePlayers();
  setupModSearch();
  window.setInterval(updateStatus, 5000);
  window.setInterval(updatePublicStatus, 10000);
  window.setInterval(updatePlayers, 10000);
})();
