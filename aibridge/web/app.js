"use strict";

const state = {
  status: null,
  repositories: [],
  selectedRepository: "",
  currentDirectory: ".",
  runs: [],
};

const byId = (id) => document.getElementById(id);

async function requestJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
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
    await Promise.all([loadFiles(state.currentDirectory), loadRuns()]);
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