"use strict";

const state = {
  status: null,
  repositories: [],
  selectedRepository: "",
  currentDirectory: ".",
  runs: [],
  agentCsrfToken: "",
  agentProvider: null,
  agentData: null,
  agentPollTimer: null,
};

const byId = (id) => document.getElementById(id);

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function setDot(element, stateName) {
  element.className = `status-dot is-${stateName}`;
}

function setMessage(container, message, isError = false) {
  const paragraph = document.createElement("p");
  paragraph.className = isError ? "empty-message error-message" : "empty-message";
  paragraph.textContent = message;
  container.replaceChildren(paragraph);
}

function setView(viewName) {
  document.querySelectorAll(".view").forEach((view) => {
    view.hidden = view.id !== `view-${viewName}`;
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === viewName;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const activeButton = document.querySelector(`.nav-item[data-view="${viewName}"]`);
  byId("current-section").textContent = activeButton?.textContent.trim().replace(/^\d+/, "").trim() || "Overview";
}

function updateStatus(status) {
  state.status = status;
  const repository = status.repository;
  byId("project-name").textContent = repository.name;
  byId("repository-path").textContent = repository.path;
  byId("workflow-count").textContent = repository.workflow_files.length;
  byId("test-count").textContent = repository.test_files.length;
  byId("readme-state").textContent = repository.has_readme ? "Present" : "Not found";
  byId("devcontainer-state").textContent = repository.has_devcontainer ? "Present" : "Not found";
  byId("python-project-state").textContent = repository.has_python_project ? "Present" : "Not found";

  byId("api-state").textContent = "Online";
  setDot(byId("sidebar-api-dot"), "online");
  byId("sidebar-api-label").textContent = "Local server online";
  setDot(byId("service-local-dot"), "online");
  byId("service-local-state").textContent = "Online";

  const github = status.github;
  if (github.connected) {
    byId("github-label").textContent = `GitHub: ${github.login}`;
    byId("github-state").textContent = "Connected";
    byId("github-note").textContent = "Authenticated via GitHub CLI";
    byId("service-github-state").textContent = "Connected";
    setDot(byId("github-dot"), "online");
    setDot(byId("service-github-dot"), "online");
  } else {
    byId("github-label").textContent = "GitHub: not connected";
    byId("github-state").textContent = "Offline";
    byId("github-note").textContent = github.message;
    byId("service-github-state").textContent = "Not connected";
    setDot(byId("github-dot"), "offline");
    setDot(byId("service-github-dot"), "offline");
  }

  byId("agent-message").textContent = status.agents.message;
  byId("service-agent-state").textContent = status.agents.simulation
    ? "Simulation (tests only)"
    : status.agents.enabled
      ? "Ready · approval required"
      : status.agents.configured
        ? "Configured · disabled"
        : "Not configured";
  setDot(byId("service-agent-dot"), status.agents.enabled ? "online" : "offline");
  byId("last-updated").textContent = `Checked ${new Date().toLocaleTimeString()}`;
}

function formatDate(value) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Date unavailable" : date.toLocaleString();
}

function stateLabel(run) {
  return run.conclusion || run.status || "Unknown";
}

function stateClass(run) {
  if (run.conclusion === "success") return "is-success";
  if (["failure", "cancelled", "timed_out", "action_required"].includes(run.conclusion)) return "is-failure";
  if (["in_progress", "queued", "requested", "waiting"].includes(run.status)) return "is-in-progress";
  return "";
}

function createRunRow(run, showLogs) {
  const row = document.createElement("article");
  row.className = "run-row";

  const main = document.createElement("div");
  main.className = "run-main";
  const title = document.createElement("span");
  title.className = "run-title";
  title.textContent = run.name || run.display_title || `Workflow run #${run.id}`;
  const meta = document.createElement("span");
  meta.className = "run-meta";
  [run.head_branch, run.event, formatDate(run.created_at)].filter(Boolean).forEach((value) => {
    const detail = document.createElement("span");
    detail.textContent = value;
    meta.append(detail);
  });
  main.append(title, meta);

  const result = document.createElement("span");
  result.className = `run-state ${stateClass(run)}`;
  result.textContent = stateLabel(run);
  row.append(main, result);

  if (showLogs) {
    const logsButton = document.createElement("button");
    logsButton.className = "logs-button";
    logsButton.type = "button";
    logsButton.textContent = "View logs";
    logsButton.addEventListener("click", () => showRunLogs(run));
    row.append(logsButton);
  }
  return row;
}

function renderOverviewRuns(runs, message = "") {
  const container = byId("overview-runs");
  if (message) {
    setMessage(container, message, message.startsWith("GitHub CLI"));
    return;
  }
  if (!runs.length) {
    setMessage(container, "No workflow runs were returned for the selected repository.");
    return;
  }
  container.replaceChildren(...runs.slice(0, 4).map((run) => createRunRow(run, true)));
}

function renderActionRuns(runs, message = "") {
  const container = byId("actions-runs");
  if (message) {
    setMessage(container, message, true);
    byId("run-total").textContent = "Unavailable";
    return;
  }
  byId("run-total").textContent = `${runs.length} returned`;
  if (!runs.length) {
    setMessage(container, "No workflow runs were returned for this repository.");
    return;
  }
  container.replaceChildren(...runs.map((run) => createRunRow(run, true)));
}

async function loadRuns() {
  const repository = state.selectedRepository;
  if (!repository) {
    renderOverviewRuns([], "Select a repository after connecting GitHub.");
    renderActionRuns([], "Connect GitHub and select a repository to load workflow runs.");
    return;
  }
  byId("overview-actions-context").textContent = `Live data from ${repository}`;
  try {
    const result = await requestJson(`/api/actions?repo=${encodeURIComponent(repository)}`);
    state.runs = result.runs;
    renderOverviewRuns(result.runs);
    renderActionRuns(result.runs);
  } catch (error) {
    state.runs = [];
    renderOverviewRuns([], error.message);
    renderActionRuns([], error.message);
  }
}

async function showRunLogs(run) {
  const dialog = byId("detail-dialog");
  byId("dialog-eyebrow").textContent = `GITHUB ACTIONS / RUN ${run.id}`;
  byId("dialog-title").textContent = run.name || run.display_title || `Workflow run #${run.id}`;
  byId("dialog-content").textContent = "Loading logs from GitHub...";
  dialog.showModal();
  try {
    const parameters = new URLSearchParams({ repo: state.selectedRepository, run_id: String(run.id) });
    const result = await requestJson(`/api/action-logs?${parameters}`);
    byId("dialog-content").textContent = result.logs || "GitHub returned no log output for this run.";
  } catch (error) {
    byId("dialog-content").textContent = `Logs unavailable: ${error.message}`;
  }
}

function renderFiles(payload) {
  state.currentDirectory = payload.path;
  byId("explorer-path").textContent = payload.path;
  byId("up-directory").disabled = payload.path === ".";
  const container = byId("file-list");
  if (!payload.entries.length) {
    setMessage(container, "This directory is empty.");
    return;
  }
  const buttons = payload.entries.map((entry) => {
    const button = document.createElement("button");
    button.className = "file-entry";
    button.type = "button";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = entry.name;
    const kind = document.createElement("span");
    kind.className = "file-kind";
    kind.textContent = entry.kind === "directory" ? "Directory" : `${entry.size} B`;
    button.append(name, kind);
    button.addEventListener("click", () => {
      if (entry.kind === "directory") loadFiles(entry.path);
      else showFile(entry.path);
    });
    return button;
  });
  container.replaceChildren(...buttons);
}

async function loadFiles(path = ".") {
  const container = byId("file-list");
  setMessage(container, "Loading local files...");
  try {
    const result = await requestJson(`/api/files?path=${encodeURIComponent(path)}`);
    renderFiles(result);
  } catch (error) {
    setMessage(container, error.message, true);
  }
}

async function showFile(path) {
  const dialog = byId("detail-dialog");
  byId("dialog-eyebrow").textContent = "LOCAL WORKING COPY / READ ONLY";
  byId("dialog-title").textContent = path;
  byId("dialog-content").textContent = "Loading file...";
  dialog.showModal();
  try {
    const result = await requestJson(`/api/file?path=${encodeURIComponent(path)}`);
    byId("dialog-content").textContent = result.content;
  } catch (error) {
    byId("dialog-content").textContent = `Preview unavailable: ${error.message}`;
  }
}

function renderRepositories(repositories, message = "") {
  const container = byId("github-repository-list");
  if (message) {
    setMessage(container, message, true);
    return;
  }
  if (!repositories.length) {
    setMessage(container, "No repositories were returned for this account.");
    return;
  }
  const entries = repositories.map((repository) => {
    const button = document.createElement("button");
    button.className = "github-repository-entry";
    button.type = "button";
    const main = document.createElement("span");
    main.className = "repo-entry-main";
    const name = document.createElement("span");
    name.className = "repo-entry-name";
    name.textContent = repository.full_name;
    const meta = document.createElement("span");
    meta.className = "repo-entry-meta";
    meta.textContent = repository.default_branch ? `Default branch: ${repository.default_branch}` : "Default branch unavailable";
    const visibility = document.createElement("span");
    visibility.className = "repo-visibility";
    visibility.textContent = repository.private ? "Private" : "Public";
    main.append(name, meta);
    button.append(main, visibility);
    button.addEventListener("click", () => {
      state.selectedRepository = repository.full_name;
      byId("actions-repository").value = repository.full_name;
      setView("actions");
      loadRuns();
    });
    return button;
  });
  container.replaceChildren(...entries);
}

function renderAgentTasks(tasks) {
  const container = byId("agent-tasks");
  byId("task-total").textContent = String(tasks.length);
  if (!tasks.length) {
    setMessage(container, "No local tasks yet. New instructions are saved pending approval.");
    return;
  }

  const entries = tasks.map((task) => {
    const article = document.createElement("article");
    article.className = "agent-task";
    const heading = document.createElement("div");
    heading.className = "agent-task-heading";
    const identifier = document.createElement("strong");
    identifier.textContent = `TASK ${task.id.slice(0, 8)}`;
    const status = document.createElement("span");
    status.className = `run-state is-${task.status.replaceAll("_", "-")}`;
    status.textContent = task.status.replaceAll("_", " ");
    heading.append(identifier, status);

    const prompt = document.createElement("p");
    prompt.className = "agent-task-prompt";
    prompt.textContent = task.instruction;
    const meta = document.createElement("div");
    meta.className = "run-meta";
    const provider = document.createElement("span");
    provider.textContent = task.provider_id
      ? `${task.provider_id}${task.selected_model ? ` · ${task.selected_model}` : ""}`
      : "Provider not selected";
    const created = document.createElement("span");
    created.textContent = formatDate(task.created_at);
    meta.append(provider, created);
    article.append(heading, prompt, meta);

    if (task.result) {
      const result = document.createElement("pre");
      result.className = "agent-task-result";
      result.textContent = task.result;
      article.append(result);
    }
    if (task.error) {
      const error = document.createElement("p");
      error.className = "agent-task-error";
      error.textContent = task.error;
      article.append(error);
    }
    if (["awaiting_approval", "failed"].includes(task.status)) {
      const approve = document.createElement("button");
      approve.className = "logs-button";
      approve.type = "button";
      approve.textContent = task.status === "failed" ? "Review & retry" : "Approve & send";
      approve.disabled = !state.agentData?.providers?.some((item) => item.enabled);
      approve.title = approve.disabled
        ? "No verified free provider is currently enabled."
        : "Review the providers that may receive this instruction.";
      approve.addEventListener("click", () => approveAgentTask(task));
      article.append(approve);
    }
    return article;
  });
  container.replaceChildren(...entries);
}

function renderRouter(data) {
  const providers = Array.isArray(data.providers) ? data.providers : [];
  const health = new Map((data.health || []).map((item) => [item.provider_id, item]));
  const settings = data.settings || {};
  const enabledIds = Array.isArray(settings.enabled_provider_ids)
    ? settings.enabled_provider_ids
    : providers.filter((provider) => provider.enabled).map((provider) => provider.id);
  const eligible = providers.filter((provider) => provider.eligible ?? provider.configured);
  byId("router-mode").value = settings.mode || "automatic";
  byId("router-mode-state").textContent = data.free_only
    ? "FREE_ONLY · PAID ROUTES BLOCKED"
    : "FREE_ONLY";

  const manualSelect = byId("router-manual-provider");
  const previousManual = settings.manual_provider_id || "";
  manualSelect.replaceChildren();
  eligible.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = `${provider.name} · ${provider.model || "model unverified"}`;
    manualSelect.append(option);
  });
  if (!eligible.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No eligible free provider";
    manualSelect.append(option);
  }
  manualSelect.value = eligible.some((provider) => provider.id === previousManual)
    ? previousManual
    : eligible[0]?.id || "";

  const providerList = byId("router-provider-list");
  if (!providers.length) {
    setMessage(providerList, "Provider status is unavailable until authorization.");
  } else {
    const entries = providers.map((provider) => {
      const card = document.createElement("article");
      card.className = "router-provider";
      const main = document.createElement("label");
      main.className = "router-provider-main";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "router-provider-enabled";
      checkbox.value = provider.id;
      checkbox.checked = enabledIds.includes(provider.id);
      checkbox.disabled = !(provider.eligible ?? provider.configured);
      const name = document.createElement("span");
      name.textContent = provider.name;
      main.append(checkbox, name);

      const priorityLabel = document.createElement("label");
      priorityLabel.textContent = "USER PRIORITY";
      const priority = document.createElement("input");
      priority.type = "number";
      priority.min = "-10";
      priority.max = "10";
      priority.step = "1";
      priority.className = "router-priority";
      priority.dataset.providerId = provider.id;
      priority.value = settings.priorities?.[provider.id] ?? "0";
      priority.disabled = !(provider.eligible ?? provider.configured);
      priorityLabel.append(priority);

      const model = document.createElement("span");
      model.className = "router-provider-model";
      const providerHealth = health.get(provider.id);
      const availability = providerHealth
        ? providerHealth.state === "cooldown"
          ? `cooldown · retry in ${providerHealth.retry_after_seconds}s`
          : providerHealth.state
        : provider.availability || "unknown";
      const contextWindow = provider.context_window_tokens
        ? `${provider.context_window_tokens.toLocaleString()} tokens`
        : "not verified";
      model.textContent = `Model: ${provider.model || "not verified"} · Capabilities: ${(provider.capabilities || ["text"]).join(", ")} · Context: ${contextWindow} · State: ${availability}`;
      const details = document.createElement("span");
      details.className = `router-provider-note ${provider.eligible ?? provider.configured ? "" : "is-error"}`;
      details.textContent = `${provider.message} ${provider.quota || ""} Local today: ${provider.local_requests_today || 0} requests / ${provider.local_estimated_tokens_today || 0} estimated tokens${provider.id === "cloudflare" ? ` / ${provider.local_estimated_neurons_today || 0} estimated neurons` : ""}.`;
      card.append(main, priorityLabel, model, details);
      return card;
    });
    providerList.replaceChildren(...entries);
  }

  const attempts = byId("router-attempts");
  if (!data.attempts?.length) {
    setMessage(attempts, "No provider attempts recorded.");
  } else {
    attempts.replaceChildren(...data.attempts.slice(0, 12).map((attempt) => {
      const row = document.createElement("div");
      row.className = "router-attempt";
      const provider = document.createElement("strong");
      provider.textContent = `${attempt.provider_id} · ${attempt.model}`;
      const outcome = document.createElement("span");
      outcome.textContent = `${attempt.outcome} · ${attempt.latency_ms} ms`;
      const reason = document.createElement("small");
      reason.textContent = attempt.reason || formatDate(attempt.created_at);
      row.append(provider, outcome, reason);
      return row;
    }));
  }
}

async function loadAgentWorkspace() {
  try {
    const session = await requestJson("/api/session");
    state.agentCsrfToken = session.csrf_token;
    byId("agent-login-section").hidden = session.authenticated;
    byId("agent-columns").hidden = !session.authenticated;
    if (!session.authenticated) {
      state.agentProvider = null;
      state.agentData = null;
      byId("agent-provider-name").textContent = "Task workspace locked";
      byId("agent-message").textContent =
        "Enter the one-session access code printed by the server to view and manage local tasks.";
      byId("agent-badge").className = "status-badge is-offline";
      byId("agent-badge").textContent = "AUTHORIZATION REQUIRED";
      return;
    }

    const data = await requestJson("/api/agents");
    state.agentData = data;
    state.agentProvider = data.provider;
    byId("agent-provider-name").textContent = data.provider.name;
    byId("agent-message").textContent = data.provider.message;
    const badge = byId("agent-badge");
    badge.className = `status-badge ${data.provider.simulation ? "is-simulation" : data.provider.enabled ? "is-online" : "is-offline"}`;
    badge.textContent = data.provider.simulation
      ? "SIMULATION — TESTS ONLY"
      : data.provider.enabled
        ? "FREE ROUTE — APPROVAL REQUIRED"
        : data.provider.configured
          ? "CONFIGURED — DISABLED"
          : "NO VERIFIED FREE ROUTE";
    renderRouter(data);
    renderAgentTasks(data.tasks);
    window.clearTimeout(state.agentPollTimer);
    if (data.tasks.some((task) => ["approved", "running"].includes(task.status))) {
      state.agentPollTimer = window.setTimeout(loadAgentWorkspace, 1200);
    }
  } catch (error) {
    byId("agent-provider-name").textContent = "Provider status unavailable";
    byId("agent-message").textContent = error.message;
    setMessage(byId("agent-tasks"), error.message, true);
  }
}

async function saveRouterSettings() {
  const feedback = byId("router-feedback");
  feedback.classList.remove("is-error");
  const enabledProviderIds = [...byId("router-provider-list").querySelectorAll(".router-provider-enabled:checked")]
    .map((input) => input.value);
  const priorities = {};
  byId("router-provider-list").querySelectorAll(".router-priority").forEach((input) => {
    const value = Number(input.value);
    priorities[input.dataset.providerId] = Number.isFinite(value)
      ? Math.max(-10, Math.min(10, Math.trunc(value)))
      : 0;
  });
  const manualProvider = byId("router-manual-provider").value;
  if (byId("router-mode").value === "manual" && !enabledProviderIds.includes(manualProvider)) {
    feedback.classList.add("is-error");
    feedback.textContent = "Enable the preferred provider before saving manual mode.";
    return;
  }
  try {
    await requestJson("/api/agents/router", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.agentCsrfToken,
      },
      body: JSON.stringify({
        mode: byId("router-mode").value,
        manual_provider_id: manualProvider || null,
        enabled_provider_ids: enabledProviderIds,
        priorities,
      }),
    });
    feedback.textContent = "Routing preferences saved locally.";
    await loadAgentWorkspace();
  } catch (error) {
    feedback.classList.add("is-error");
    feedback.textContent = error.message;
  }
}

async function approveAgentTask(task) {
  if (!state.agentData?.providers?.some((item) => item.enabled)) return;
  let candidates;
  let plan;
  try {
    plan = await requestJson("/api/agents/plan", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.agentCsrfToken,
      },
      body: JSON.stringify({ instruction: task.instruction }),
    });
    candidates = plan.candidates;
  } catch (error) {
    byId("task-feedback").classList.add("is-error");
    byId("task-feedback").textContent = error.message;
    return;
  }
  if (!candidates?.length) {
    byId("task-feedback").classList.add("is-error");
    byId("task-feedback").textContent = "No hay proveedores gratuitos disponibles para esta tarea.";
    return;
  }
  const providerLabels = candidates.map((candidate) => {
    const provider = state.agentData.providers.find((item) => item.id === candidate.provider_id);
    return `${provider?.name || candidate.provider_id} (${candidate.model}) — ${candidate.explanation}`;
  });
  const approved = window.confirm(
    `Task type: ${plan.task_type}\n\nThis sends the instruction to the following free-only provider(s), in order:\n\n${providerLabels.join("\n")}\n\nThe instruction may contain private information. I authorize these providers to receive it. No files or commands will be changed or executed. Continue?`,
  );
  if (!approved) return;

  const button = [...byId("agent-tasks").querySelectorAll("button")]
    .find((candidate) => candidate.textContent.includes("Approve") || candidate.textContent.includes("Review"));
  if (button) button.disabled = true;
  try {
    const result = await requestJson(`/api/agents/tasks/${encodeURIComponent(task.id)}/approve`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.agentCsrfToken,
      },
      body: JSON.stringify({
        provider_ids: candidates.map((candidate) => candidate.provider_id),
        external_consent: true,
      }),
    });
    const feedback = byId("task-feedback");
    feedback.classList.remove("is-error");
    feedback.textContent = `Task ${result.task.status}; provider call queued.`;
    await loadAgentWorkspace();
  } catch (error) {
    byId("task-feedback").classList.add("is-error");
    byId("task-feedback").textContent = error.message;
    await loadAgentWorkspace();
  }
}

function updateRepositorySelect(repositories) {
  const select = byId("actions-repository");
  const previous = state.selectedRepository;
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select a repository";
  select.append(placeholder);
  repositories.forEach((repository) => {
    const option = document.createElement("option");
    option.value = repository.full_name;
    option.textContent = repository.full_name;
    select.append(option);
  });
  state.selectedRepository = repositories.some((repo) => repo.full_name === previous)
    ? previous
    : repositories[0]?.full_name || "";
  select.value = state.selectedRepository;
}

async function refreshDashboard() {
  const button = byId("refresh-button");
  button.disabled = true;
  button.textContent = "Refreshing...";
  try {
    const status = await requestJson("/api/status");
    updateStatus(status);
    if (status.github.connected) {
      try {
        const result = await requestJson("/api/repositories");
        state.repositories = result.repositories;
        renderRepositories(state.repositories);
        updateRepositorySelect(state.repositories);
      } catch (error) {
        state.repositories = [];
        updateRepositorySelect([]);
        renderRepositories([], error.message);
      }
    } else {
      state.repositories = [];
      updateRepositorySelect([]);
      renderRepositories([], `${status.github.message} Inicia sesión con gh auth login en la terminal y vuelve a actualizar.`);
    }
    await Promise.all([loadFiles(state.currentDirectory), loadRuns(), loadAgentWorkspace()]);
  } catch (error) {
    byId("api-state").textContent = "Unavailable";
    byId("project-name").textContent = "Local server unavailable";
    byId("sidebar-api-label").textContent = "Local server unavailable";
    setDot(byId("sidebar-api-dot"), "offline");
    setDot(byId("service-local-dot"), "offline");
    byId("service-local-state").textContent = "Unavailable";
    renderOverviewRuns([], error.message);
    renderActionRuns([], error.message);
    setMessage(byId("file-list"), error.message, true);
  } finally {
    byId("refresh-button").disabled = false;
    byId("refresh-button").textContent = "Refresh";
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});
document.querySelectorAll("[data-navigate]").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.navigate));
});
byId("refresh-button").addEventListener("click", refreshDashboard);
byId("router-save").addEventListener("click", saveRouterSettings);
byId("agent-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("agent-access-code");
  const button = byId("agent-login-button");
  const feedback = byId("agent-login-feedback");
  button.disabled = true;
  feedback.classList.remove("is-error");
  feedback.textContent = "Checking access code...";
  try {
    await requestJson("/api/agents/auth", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.agentCsrfToken,
      },
      body: JSON.stringify({ access_code: input.value }),
    });
    input.value = "";
    feedback.textContent = "Authorized.";
    await loadAgentWorkspace();
  } catch (error) {
    feedback.classList.add("is-error");
    feedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
byId("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("task-instruction");
  const button = byId("task-submit");
  const feedback = byId("task-feedback");
  button.disabled = true;
  feedback.classList.remove("is-error");
  feedback.textContent = "Saving task locally...";
  try {
    await requestJson("/api/agents/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.agentCsrfToken,
      },
      body: JSON.stringify({ instruction: input.value }),
    });
    input.value = "";
    feedback.textContent = "Task saved locally and is awaiting approval.";
    await loadAgentWorkspace();
  } catch (error) {
    feedback.classList.add("is-error");
    feedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
byId("actions-repository").addEventListener("change", (event) => {
  state.selectedRepository = event.target.value;
  loadRuns();
});
byId("up-directory").addEventListener("click", () => {
  const segments = state.currentDirectory.split("/").filter(Boolean);
  segments.pop();
  loadFiles(segments.join("/") || ".");
});
byId("close-dialog").addEventListener("click", () => byId("detail-dialog").close());

refreshDashboard();