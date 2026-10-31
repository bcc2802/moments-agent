"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const viewKey = "moments-cloud-view:" + window.location.origin;
  const viewNames = { start: "工作台", profile: "个人说明书", history: "素材库", write: "工作台", library: "我的内容" };
  const formIds = {
    author: { identity: "author-identity" },
    audience: { people: "audience-people" },
    preferences: { tone: "preferences-tone" }
  };
  const requiredProfileLabels = { "author-identity": "我的身份与业务", "audience-people": "我想写给谁", "preferences-tone": "我希望怎样表达" };
  const selectedPresets = new Map();
  const fallbackPresets = [
    { id: "colloquial", label: "更口语化一点", description: "像平时说话，短句、自然，不改变原有事实。" },
    { id: "authentic", label: "更真实一点", description: "删去空泛包装，保留真实细节与未知，不补造经历。" },
    { id: "professional", label: "更专业一点", description: "说清判断、依据与边界，不堆术语或夸大结论。" }
  ];

  let csrfToken = "";
  let state = null;
  let busy = false;
  let activeJob = null;
  let profileDirty = false;
  let profilesLoaded = false;
  let selectedVariant = null;
  let activeView = "start";
  let toastTimer;
  let historyReading = null;
  let loadedFile = null;
  let pollTimer = null;
  let jobStartedAt = null;
  let jobClock = null;
  let submissionUncertain = false;
  let completedJobTarget = null;
  const angleTypes = {
    1: { key: "process", title: "切口一 · 讲过程", icon: "↗", description: "把事情怎样发生、怎样推进说清楚。" },
    2: { key: "judgment", title: "切口二 · 说判断", icon: "◇", description: "把你的观点、依据和边界说清楚。" },
    3: { key: "reader", title: "切口三 · 代入读者场景", icon: "◎", description: "从读者会遇到的问题，带出这次的提醒。" }
  };
  const phaseLabels = { queued: "等待执行", preparing: "整理本次材料", requesting: "等待模型响应", receiving: "接收模型结果", validating: "校验文案格式", saving: "保存并回读稿件" };
  const manualEdits = new Map();
  const revisionEdits = new Map();
  const openDetails = new Set();

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function button(text, className, action) {
    const item = node("button", className, text);
    item.type = "button";
    item.addEventListener("click", action);
    return item;
  }

  function mutationButton(text, className, action) {
    const item = button(text, className, action);
    item.dataset.mutation = "true";
    item.disabled = busy;
    return item;
  }

  function toast(message, error = false) {
    window.clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").className = "toast" + (error ? " error" : "");
    $("toast").hidden = false;
    toastTimer = window.setTimeout(() => { $("toast").hidden = true; }, error ? 7000 : 4500);
  }

  function explain(error) {
    const message = error && error.message ? error.message : "操作未完成，请刷新检查写作服务状态。";
    return error && error.code ? message + "（" + error.code + "）" : message;
  }

  function alertMessage(message) {
    $("global-alert").textContent = message || "";
    $("global-alert").hidden = !message;
  }

  function navigate(view, focus = true) {
    if (view === "write") view = "start";
    if (!(view in viewNames)) view = "start";
    activeView = view;
    document.querySelectorAll(".view").forEach((item) => { item.hidden = item.id !== "view-" + view; });
    document.querySelectorAll("[data-view]").forEach((item) => {
      const selected = item.dataset.view === view;
      item.classList.toggle("active", selected);
      if (selected) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $("breadcrumb").textContent = viewNames[view];
    $("sidebar").classList.remove("open");
    $("menu-toggle").setAttribute("aria-expanded", "false");
    try { window.sessionStorage.setItem(viewKey, view); } catch (_) { /* Navigation remains usable. */ }
    if (focus) {
      $("main").focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "auto" });
    }
  }

  async function api(path, payload) {
    if (payload !== undefined && !csrfToken) {
      const error = new Error("工作区会话尚未准备完成，请刷新页面后重试。");
      error.code = "SESSION_REQUIRED";
      error.httpStatus = 403;
      throw error;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 45000);
    try {
      const options = {
        method: payload === undefined ? "GET" : "POST",
        headers: {},
        cache: "no-store", credentials: "same-origin", redirect: "error", signal: controller.signal
      };
      if (payload !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.headers["X-Moments-CSRF"] = csrfToken;
        options.body = JSON.stringify(payload);
      }
      const response = await fetch("/moments" + path, options);
      let result;
      try { result = await response.json(); }
      catch (_) { throw new Error("写作服务返回了无法读取的结果，请检查服务是否仍在运行。"); }
      if (path === "/api/state" && typeof result.csrf_token === "string") csrfToken = result.csrf_token;
      if (state && result.quota) { state.quota = result.quota; renderQuota(); }
      if (!response.ok || (result.status === "ERROR" && !path.startsWith("/api/jobs/"))) {
        const error = new Error(result.message || "本次操作没有完成。");
        error.code = result.code || "REQUEST_FAILED";
        error.httpStatus = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("写作服务暂未响应。请刷新检查状态，不要重复提交同一写作请求。");
      if (error instanceof TypeError) throw new Error("暂时连接不到写作服务，请检查网络后刷新。");
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  function quotaExhausted() {
    return Boolean(state && state.quota && Number.isFinite(state.quota.remaining) && state.quota.remaining <= 0);
  }

  function modelReady() {
    return Boolean(state && state.model && state.model.available === true && state.model.authenticated === true);
  }

  function onboardingState() {
    if (state && state.onboarding) return state.onboarding;
    const history = state && state.history || {};
    const count = Number.isFinite(history.matched_files) ? history.matched_files : (history.items || []).length;
    const profile = Boolean(state && state.profile_ready);
    return { profile_ready: profile, history_ready: count > 0, history_count: count, ready: profile };
  }

  function updateButtons() {
    document.querySelectorAll("[data-mutation]").forEach((item) => { item.disabled = busy; });
    $("save-profile-button").disabled = busy || !state;
    $("import-history-button").disabled = busy || !state;
    $("generate-button").disabled = busy || !modelReady() || !onboardingState().ready || quotaExhausted();
    $("model-provider").disabled = busy || !state;
    $("material").readOnly = busy;
    document.querySelectorAll(".publish-button").forEach((item) => {
      const checkbox = document.getElementById(item.dataset.checkbox);
      item.disabled = busy || !checkbox || !checkbox.checked;
    });
    document.querySelectorAll(".ai-revise-button").forEach((item) => { item.disabled = busy || !modelReady() || quotaExhausted(); });
  }

  function setBusy(value) {
    busy = value;
    updateButtons();
  }

  function renderModel() {
    const model = state.model || {};
    const ready = modelReady();
    const clean = state.status === "OK";
    $("connection-status").className = "status-label " + (clean ? "ready" : "error");
    $("connection-label").textContent = clean ? "云端已连接" : "需要检查";
    const label = model.label || "智谱 GLM-5.3-Flash";
    const choices = [{ id: "zhipu", label: "智谱 GLM-5.3-Flash" }];
    $("model-provider").replaceChildren(...choices.map((item) => {
      const option = node("option", "", item.label || item.id);
      option.value = item.id;
      return option;
    }));
    $("model-provider").value = "zhipu";
    $("model-selection-note").textContent = ready
      ? label + " · 使用网站提供的智谱额度"
      : (model.message || "写作服务暂未就绪，请稍后刷新。");
    $("generation-model-name").textContent = label;
    $("home-model-label").textContent = ready ? label + " 已连接" : "连接写作模型";
    $("home-model-message").textContent = ready ? "模型可用性以实际请求结果为准。" : (model.message || "写作服务暂未就绪，请稍后刷新。");
    $("write-model-status").textContent = ready ? label + " · 已连接" : "写作服务暂未就绪";
    $("model-help").hidden = ready;
    $("model-help-message").textContent = model.message || "写作服务暂未就绪，请稍后刷新。";
    renderQuota();
    $("profile-notice").textContent = onboardingState().profile_ready
      ? "说明书已保存。下次写作会参考这些资料；本次事实仍以你当次提供的材料为准。"
      : "这3项均为必填。写清身份、读者与表达偏好，AI才更容易写出贴近你的内容。";
  }

  function renderQuota() {
    const quota = state && state.quota;
    const total = quota && Number.isFinite(quota.daily_limit) ? Math.max(0, quota.daily_limit) : null;
    const remaining = quota && Number.isFinite(quota.remaining) ? Math.max(0, quota.remaining) : null;
    $("quota-label").textContent = remaining === null ? "正在读取今日可用次数" : "今日还可使用 " + remaining + " 次";
    $("quota-note").textContent = total === null
      ? "生成与润色共用；完成前请勿重复提交。"
      : "每天 " + total + " 次，生成与润色共用。全站额度用完后次日恢复。";
    $("quota-label").classList.toggle("quota-empty", remaining === 0);
  }

  function renderDashboard() {
    const setup = onboardingState();
    const profile = Boolean(setup.profile_ready);
    const history = Boolean(setup.history_ready);
    const count = Number.isFinite(setup.history_count) ? setup.history_count : 0;
    $("profile-step-status").textContent = profile ? "已完成，可随时修改" : "填写3项必填资料 →";
    $("history-step-status").textContent = history ? "已有 " + count + " 篇，可继续补充" : "可以稍后补充，不影响开始写作 →";
    $("profile-step-check").textContent = profile ? "✓" : "→";
    $("history-step-check").textContent = history ? "✓" : "→";
    $("profile-step-check").closest(".setup-step").classList.toggle("complete", profile);
    $("history-step-check").closest(".setup-step").classList.toggle("complete", history);
    document.querySelector(".onboarding").classList.toggle("ready", Boolean(setup.ready));
    $("onboarding-progress").textContent = profile ? "已可开始写作" : "1 项必填待完成";
    $("write-step-status").textContent = setup.ready ? "就在下方，写下这次的真实材料" : "准备完成后即可生成";
    $("composer-gate").hidden = Boolean(setup.ready);
    $("composer-gate").textContent = "先保存个人说明书的3项必填内容，即可开始。历史作品可稍后补充。";
  }

  function populateProfiles(force = false) {
    if (profilesLoaded && profileDirty && !force) return;
    const shouldBlank = !Boolean(state.profile_ready || onboardingState().profile_ready);
    for (const [group, fields] of Object.entries(formIds)) {
      const values = state.profiles && state.profiles[group] && state.profiles[group].fields || {};
      for (const [key, id] of Object.entries(fields)) {
        $(id).value = shouldBlank ? "" : (values[key] || "");
        $(id).removeAttribute("aria-invalid");
      }
    }
    profilesLoaded = true;
    profileDirty = false;
    $("profile-save-hint").textContent = "保存后长期使用，也可以随时修改。";
  }

  function emptyState(title, description, small = false) {
    const empty = node("div", "empty-state" + (small ? " small" : ""));
    empty.append(node("span", "empty-symbol", "▤"), node("h3", "", title), node("p", "", description));
    return empty;
  }

  function renderHistory() {
    const materials = state.materials || {};
    const originalHistory = state.history || {};
    const received = Array.isArray(materials.items) ? materials.items : (originalHistory.items || []).map((item) => ({ ...item, kind: "history" }));
    const all = received.filter((item) => item.kind === "history" || ((item.kind === "final" || item.kind === "published") && item.status === "已发布"));
    const filter = $("materials-filter").value;
    const items = all.filter((item) => filter === "all" || (filter === "reference" ? item.reference_eligible !== false : filter === "published" ? item.kind !== "history" : item.kind === "history"));
    const total = Number.isFinite(materials.total) ? materials.total : all.length;
    $("history-count").textContent = String(total);
    const limits = "最多加载" + (materials.history_limit || 10) + "篇导入作品及" + (materials.draft_limit || 30) + "篇已发终稿";
    $("history-coverage").textContent = (materials.coverage_complete === false ? "部分素材未能读取；" : "")
      + "当前加载" + all.length + "篇" + (materials.has_more ? "，还有更多" : "") + "；" + limits + "。筛选仅覆盖已加载范围。";
    const list = $("history-list");
    list.replaceChildren();
    if (!items.length) list.append(emptyState(all.length ? "当前范围没有匹配的素材" : "从第一篇历史作品开始", all.length ? "换一个素材类型再看看。" : "上传或粘贴一篇你认可的作品；明确确认已发布的终稿也会进入这里。", true));
    for (const item of items) {
      const published = item.kind === "final" || item.kind === "published";
      const row = button("", "history-item", () => openMaterial(item));
      const top = node("div", "history-item-top");
      top.append(node("span", "history-name", item.title || item.relative_path || "历史作品"), node("span", "badge " + (published ? "published" : "neutral"), published ? "已发终稿" : "导入原文"));
      row.append(top, node("span", "history-excerpt", item.excerpt || item.body || "点击查看正文"));
      row.append(node("span", "material-reference-note", item.reference_eligible === false ? "当前不作为写法参考" : "可供后续写法参考 · 不沿用旧经历"));
      list.append(row);
    }
  }

  function openMaterial(item) {
    if (item.kind === "history") { readHistory(item.id); return; }
    if (item.status !== "已发布") { toast("只有本人确认已发布的终稿可以作为此类素材。", true); return; }
    if (typeof item.body !== "string") { toast("这份素材没有返回全文，未用摘要冒充原文。", true); return; }
    historyReading = item;
    $("history-reader-title").textContent = item.title || "已发布终稿";
    $("history-reader-body").textContent = item.body;
    $("history-reader-status").textContent = "本人确认已发布 · 终稿只读";
    $("history-reader").hidden = false;
  }

  async function readHistory(id) {
    try {
      const result = await api("/api/history/" + encodeURIComponent(id));
      const item = result.item;
      if (!item || typeof item.body !== "string") throw new Error("服务没有返回历史全文，未用摘录冒充原文。");
      historyReading = item;
      $("history-reader-title").textContent = item.relative_path || "历史原文";
      $("history-reader-body").textContent = item.body;
      $("history-reader-status").textContent = "导入原文 · 不推断发表状态";
      $("history-reader").hidden = false;
    } catch (error) { toast(explain(error), true); }
  }

  function badges(note) {
    const row = node("div", "draft-badges");
    const status = note.status || (Array.isArray(note.tags) && note.tags[0]) || "状态未知";
    row.append(node("span", "badge " + (status === "已发布" ? "published" : "pending"), status));
    if (note.kept) row.append(node("span", "badge kept", "明确保留"));
    return row;
  }

  function angleHeader(note) {
    const type = angleTypes[Number(note.variant)];
    if (!type) return node("p", "draft-angle", note.angle || "表达切口");
    const heading = node("div", "angle-heading angle-" + type.key);
    const icon = node("span", "angle-icon", type.icon);
    icon.setAttribute("aria-hidden", "true");
    const copy = node("div", "angle-copy");
    copy.append(node("strong", "angle-title", type.title), node("p", "angle-description", type.description));
    heading.append(icon, copy);
    return heading;
  }

  function draftCard(note, roundId) {
    const editKey = roundId + ":" + note.variant;
    const card = node("article", "draft-card" + (selectedVariant === note.variant ? " selected" : ""));
    card.dataset.variant = String(note.variant);
    const top = node("div", "draft-top");
    top.append(node("span", "draft-number", "原第" + note.variant + "稿"), badges(note));
    card.append(angleHeader(note), top, node("p", "draft-angle", note.angle ? "本稿重点：" + note.angle : ""), node("h3", "", note.title || "朋友圈稿件"), node("p", "draft-body", note.body || ""));
    const tags = node("div", "draft-tags");
    for (const tag of (note.tags || []).filter((tag) => tag !== "待发布" && tag !== "已发布")) tags.append(node("span", "draft-tag", "# " + tag));
    card.append(tags);
    const actions = node("div", "draft-actions");
    const select = button(selectedVariant === note.variant ? "✓ 已选看" : "选看这条", "button secondary small", () => {
      selectedVariant = note.variant;
      renderDrafts();
      toast("已选看原第" + note.variant + "稿，未改变保留或发表状态。");
    });
    select.setAttribute("aria-pressed", String(selectedVariant === note.variant));
    actions.append(select, button("复制正文", "button secondary small", () => copyText(note.body || "")));
    card.append(actions);
    const instructionDetails = node("details");
    instructionDetails.open = openDetails.has(editKey + ":revise");
    instructionDetails.addEventListener("toggle", () => {
      if (instructionDetails.open) openDetails.add(editKey + ":revise");
      else openDetails.delete(editKey + ":revise");
    });
    instructionDetails.append(node("summary", "", "让 AI 改一改"));
    const availablePresets = Array.isArray(state.rewrite_presets) && state.rewrite_presets.length ? state.rewrite_presets : fallbackPresets;
    const presetRow = node("div", "revision-presets");
    presetRow.setAttribute("aria-label", "常用改稿选项");
    const presetDescription = node("p", "preset-description");
    const instruction = node("textarea");
    instruction.rows = 3;
    instruction.maxLength = 4000;
    instruction.value = revisionEdits.get(editKey) || "";
    instruction.addEventListener("input", () => revisionEdits.set(editKey, instruction.value));
    instruction.placeholder = "选好常用选项后，也可以补充具体修改意见";
    instruction.setAttribute("aria-label", "第" + note.variant + "稿的修改意见");
    const reviseButton = mutationButton("按我的意见修改", "button secondary small ai-revise-button", async () => {
      const preset = selectedPresets.get(editKey) || "";
      if (!preset && !instruction.value.trim()) { toast("选择一个常用选项，或写一点具体的修改意见。", true); instruction.focus(); return; }
      const payload = { round_id: roundId, variant: note.variant, instruction: instruction.value };
      if (preset) payload.preset = preset;
      await startJob("/api/revise", payload, "正在修改原第" + note.variant + "稿");
    });
    reviseButton.disabled = busy || !modelReady();
    function updatePresetChoice() {
      const selected = availablePresets.find((item) => item.id === selectedPresets.get(editKey));
      presetRow.querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.preset === (selected && selected.id))));
      presetDescription.textContent = selected ? selected.description || "只调整本稿表达，保留事实与边界。" : "先选择修改方向，再点击下方按钮。不会替你编经历。";
      reviseButton.textContent = selected ? "按“" + selected.label + "”修改" : "按我的意见修改";
    }
    for (const preset of availablePresets) {
      const chip = button(preset.label, "revision-preset", () => {
        if (selectedPresets.get(editKey) === preset.id) selectedPresets.delete(editKey);
        else selectedPresets.set(editKey, preset.id);
        updatePresetChoice();
      });
      chip.dataset.preset = preset.id;
      chip.setAttribute("aria-pressed", "false");
      presetRow.append(chip);
    }
    updatePresetChoice();
    instructionDetails.append(presetRow, presetDescription, instruction, reviseButton, node("p", "draft-helper", "只修改这条。所需原稿、说明书与意见会交给当前所选模型；修改完成仍需本人审阅。"));

    card.append(instructionDetails);
    const finalDetails = node("details");
    finalDetails.open = openDetails.has(editKey + ":final");
    finalDetails.addEventListener("toggle", () => {
      if (finalDetails.open) openDetails.add(editKey + ":final");
      else openDetails.delete(editKey + ":final");
    });
    finalDetails.append(node("summary", "", "手工修改并保存终稿"));
    const finalText = node("textarea");
    finalText.rows = 7;
    finalText.maxLength = 8000;
    finalText.value = manualEdits.has(editKey) ? manualEdits.get(editKey) : (note.body || "");
    finalText.setAttribute("aria-label", "原第" + note.variant + "稿的手工终稿");
    finalText.addEventListener("input", () => manualEdits.set(editKey, finalText.value));
    const finalButton = mutationButton("保存我的终稿", "button secondary small", async () => {
      if (!finalText.value.trim()) { toast("终稿正文不能为空。", true); return; }
      await mutate("/api/final", { round_id: roundId, variant: note.variant, body: finalText.value }, "你的终稿已保存并回读，发表状态保持原样。", () => manualEdits.delete(editKey));
    });
    finalDetails.append(finalText, finalButton, node("p", "draft-helper", "原字、标点和分行存到云端；不调用模型，也不改变发表状态。"));
    card.append(finalDetails);
    const bottom = node("div", "draft-bottom");
    if (note.status !== "已发布") {
      const keep = mutationButton(note.kept ? "✓ 已保留 · 取消保留" : "☆ 保留，留着以后发", "keep-button", async () => {
        await mutate("/api/keep", { round_id: roundId, variant: note.variant, keep: !note.kept }, note.kept ? "已取消长期保留。" : "已明确保留这篇待发稿。");
      });
      keep.setAttribute("aria-pressed", String(Boolean(note.kept)));
      const label = node("label", "publish-label");
      const check = node("input");
      check.type = "checkbox";
      check.id = "publish-check-" + note.variant;
      const checkboxText = "我已经手动发出这条朋友圈";
      label.append(check, node("span", "", checkboxText));
      const publish = mutationButton("确认已发布", "button secondary small wide publish-button", async () => {
        if (!check.checked) return;
        await mutate("/api/publish", { round_id: roundId, variant: note.variant, confirmed: true }, "已按你的报告记录发表，未操作微信。");
      });
      publish.dataset.checkbox = check.id;
      publish.disabled = true;
      check.addEventListener("change", updateButtons);
      bottom.append(keep, label, publish);
    } else {
      bottom.append(node("p", "published-copy", "✓ 已按本人明确报告记录发表。"));
      bottom.append(node("p", "draft-helper", note.published_at ? "发表时间：" + note.published_at : "实际发表时刻未提供，保持未知。"));
    }
    card.append(bottom);
    return card;
  }

  function renderDrafts() {
    const round = state && state.round;
    const notes = round && Array.isArray(round.notes) ? round.notes : [];
    if (!notes.some((note) => note.variant === selectedVariant)) selectedVariant = null;
    $("draft-grid").replaceChildren();
    for (const note of notes) $("draft-grid").append(draftCard(note, round.round_id));
    $("write-empty").hidden = Boolean(notes.length);
    $("round-heading").hidden = !notes.length;
    $("retention-note").hidden = !notes.length;
    $("round-label").textContent = notes.length ? "本轮 " + notes.length + " 篇稿件 · 已保存到云端" : "";
    if (notes.length < 3 && notes.length) $("round-heading").querySelector("h2").textContent = "本轮保留下来的稿件";
    else $("round-heading").querySelector("h2").textContent = "挑一条，继续写成自己的话";
    updateButtons();
  }

  function renderLibrary() {
    const all = state && Array.isArray(state.library) ? state.library : [];
    const query = $("library-search").value.trim().toLocaleLowerCase();
    const filter = $("library-filter").value;
    const items = all.filter((note) => {
      const text = [note.title || "", note.body || "", ...(note.tags || [])].join("\n").toLocaleLowerCase();
      return (!query || text.includes(query)) && (filter === "all" || (filter === "kept" ? note.kept : note.status === filter));
    });
    $("library-scope").textContent = "显示最近最多" + (state && state.library_limit || 10) + "篇实际稿件；搜索仅覆盖当前加载的内容，不代表全库检索。";
    const list = $("library-list");
    list.replaceChildren();
    if (!items.length) list.append(emptyState(all.length ? "当前范围没有匹配的稿件" : "内容库还是空的", all.length ? "试着换一个关键词或筛选条件。" : "第一轮稿件保存后，会出现在这里。"));
    for (const note of items) {
      const card = node("article", "library-card");
      const top = node("div", "library-meta");
      top.append(node("h3", "", note.title || "朋友圈稿件"), badges(note));
      const foot = node("div", "library-foot");
      let savedLabel = "云端存档" + (note.variant ? " · 原第" + note.variant + "稿" : "");
      if (note.status === "已发布") savedLabel += note.published_at ? " · " + note.published_at : " · 实际发表时刻未知";
      foot.append(node("span", "", savedLabel), button("复制正文", "text-button", () => copyText(note.body || "")));
      card.append(angleHeader(note), top, node("p", "draft-angle", note.angle ? "本稿重点：" + note.angle : ""), node("p", "draft-body", note.body || ""), foot);
      list.append(card);
    }
  }

  async function loadState(options = {}) {
    const result = await api("/api/state");
    state = result;
    alertMessage("");
    renderModel();
    renderDashboard();
    populateProfiles(Boolean(options.forceProfile));
    renderHistory();
    renderDrafts();
    renderLibrary();
    if (result.active_job && !activeJob) {
      setBusy(true);
      activeJob = result.active_job;
      if (jobStartedAt === null) jobStartedAt = Date.now();
      showJob("已恢复 AI 工作任务", "正在读取云端仍在执行的任务状态，请稍候。", "running");
      startJobClock();
      pollJob(activeJob);
    }
    updateButtons();
    return result;
  }

  async function mutate(path, payload, message, onSuccess) {
    if (busy) return;
    setBusy(true);
    try {
      await api(path, payload);
      if (onSuccess) onSuccess();
      await loadState();
      toast(message);
    } catch (error) { toast(explain(error), true); }
    finally { setBusy(Boolean(activeJob) || submissionUncertain); }
  }

  function modelLabel() {
    const model = state && state.model || {};
    return model.label || "智谱 GLM-5.3-Flash";
  }

  function showJob(title, message, status = "running") {
    $("job-status").hidden = false;
    $("job-status").className = "job-status floating-job " + status;
    $("job-status").setAttribute("aria-busy", String(status === "running" || status === "checking"));
    $("job-title").textContent = title;
    $("job-message").textContent = message;
    $("job-dismiss").hidden = status === "running" || status === "checking";
    $("job-symbol").textContent = status === "failed" ? "!" : "✓";
    $("job-safety-note").textContent = status === "done" ? "已保存到云端，仍需本人审阅" : status === "failed" ? "未自动重试模型请求" : "任务进行中，请勿重复提交";
    $("job-result").hidden = status !== "done";
  }

  function renderElapsed() {
    if (jobStartedAt === null) return;
    const seconds = Math.max(0, Math.floor((Date.now() - jobStartedAt) / 1000));
    const elapsed = seconds >= 60 ? Math.floor(seconds / 60) + "分" + seconds % 60 + "秒" : seconds + "秒";
    $("job-elapsed").textContent = "已用时 " + elapsed;
  }

  function startJobClock() {
    window.clearInterval(jobClock);
    renderElapsed();
    jobClock = window.setInterval(renderElapsed, 1000);
  }

  function targetLabel(target, kind) {
    if (typeof target === "string" && target) return target;
    const variant = typeof target === "number" ? target : target && target.variant;
    return variant ? "润色原第" + variant + "稿" : kind === "revision" || kind === "revise" ? "润色文案" : "生成三个切口";
  }

  function renderJob(job) {
    const started = Date.parse(job.created_at || job.started_at || "");
    if (Number.isFinite(started)) jobStartedAt = started;
    $("job-model").textContent = job.model_label || modelLabel();
    $("job-target").textContent = targetLabel(job.target, job.kind);
    $("job-phase").textContent = phaseLabels[job.phase] || (job.status === "QUEUED" ? phaseLabels.queued : "AI 正在处理本次请求");
    renderElapsed();
  }

  function showWarnings(warnings) {
    const lines = (Array.isArray(warnings) ? warnings : []).map((item) => typeof item === "string" ? item : (item.message || item.reason || item.code || "部分历史没有进入这次模型上下文。"));
    $("context-warning").textContent = lines.length ? "本次材料范围提示：\n" + lines.join("\n") : "";
    $("context-warning").hidden = !lines.length;
  }

  async function startJob(path, payload, title) {
    if (busy) return;
    setBusy(true);
    jobStartedAt = Date.now();
    completedJobTarget = { variant: payload.variant, round_id: payload.round_id };
    $("job-model").textContent = modelLabel();
    $("job-target").textContent = payload.variant ? "润色原第" + payload.variant + "稿" : "生成三个切口";
    $("job-phase").textContent = "正在提交请求";
    showWarnings([]);
    showJob(title, "已收到你的操作，正在提交给云端写作服务。可切换页面查看其他内容。", "running");
    startJobClock();
    try {
      const result = await api(path, payload);
      if (!result.job_id) throw new Error("没有收到写作任务编号，未把请求视为完成；请刷新检查状态。");
      activeJob = result.job_id;
      showWarnings(result.context_warnings);
      pollJob(activeJob);
    } catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500 && ![408, 409].includes(error.httpStatus)) {
        activeJob = null;
        window.clearInterval(jobClock);
        $("job-phase").textContent = "请求未受理";
        showJob("这次写作没有开始", explain(error), "failed");
        await loadState().catch(() => {});
        setBusy(false);
      } else {
        submissionUncertain = true;
        showJob("正在确认任务状态", explain(error) + " 正在只读检查任务是否已受理，不会重复提交。", "checking");
        recoverSubmission();
      }
    }
  }

  async function recoverSubmission() {
    window.clearTimeout(pollTimer);
    if (!submissionUncertain) return;
    try {
      const result = await loadState();
      submissionUncertain = false;
      if (result.active_job) return;
      window.clearInterval(jobClock);
      $("job-phase").textContent = "已重新连接云端";
      showJob("请核对云端稿件", "云端当前没有正在执行的任务。已重新读取稿件，请核对结果后再决定是否重新提交。", "failed");
      setBusy(false);
    } catch (_) {
      showJob("正在确认任务状态", "暂时连接不到写作服务，正在只读重查。任务结果尚未确认，写作按钮保持锁定，不会自动重发。", "checking");
      pollTimer = window.setTimeout(recoverSubmission, 3000);
    }
  }

  async function pollJob(id) {
    window.clearTimeout(pollTimer);
    if (activeJob !== id) return;
    try {
      const job = await api("/api/jobs/" + encodeURIComponent(id));
      renderJob(job);
      if (job.status === "DONE") {
        activeJob = null;
        submissionUncertain = false;
        window.clearInterval(jobClock);
        showWarnings(job.result && job.result.context_warnings);
        completedJobTarget = job.target || completedJobTarget;
        await loadState();
        setBusy(false);
        $("job-phase").textContent = "已完成 · 已保存并回读";
        showJob("AI 文案已完成", "稿件已保存。请检查事实与语气，确认采用后再手动发表。", "done");
        toast("稿件已保存并重新读取。请检查事实、语气和切口，再决定采用哪条。");
        if (activeView === "start") $("round-heading").scrollIntoView({ behavior: "smooth", block: "start" });
      } else if (job.status === "ERROR") {
        activeJob = null;
        submissionUncertain = false;
        window.clearInterval(jobClock);
        const error = typeof job.error === "string" ? new Error(job.error) : Object.assign(new Error(job.error && job.error.message || "模型任务没有完成。"), { code: job.error && job.error.code });
        $("job-phase").textContent = "本次任务未完成";
        showJob("本轮没有完成", explain(error), "failed");
        await loadState().catch(() => {});
        setBusy(false);
      } else if (job.status === "QUEUED" || job.status === "RUNNING") {
        showJob("AI 正在工作", job.message || (job.status === "QUEUED" ? "云端已受理，正在等待执行。" : "模型正在处理本次材料，完成后会校验、保存并回读。"), "running");
        pollTimer = window.setTimeout(() => pollJob(id), 1600);
      } else throw new Error("任务状态无法识别，请刷新回读云端稿件。");
    } catch (error) {
      if (activeJob !== id) {
        $("job-phase").textContent = "结果已收到，正在恢复页面读取";
        showJob("任务已结束，页面待刷新", explain(error) + " 请点顶部刷新回读稿件，不会自动重发模型请求。", "failed");
        setBusy(false);
        return;
      }
      if (error.httpStatus === 404) {
        try {
          const result = await loadState();
          if (!result.active_job) {
            activeJob = null;
            submissionUncertain = false;
            window.clearInterval(jobClock);
            $("job-phase").textContent = "云端已没有此任务记录";
            showJob("请核对云端稿件", "写作服务已没有这次任务的记录。已回读现有稿件；结果仍需核对，不会自动重发模型请求。", "failed");
            setBusy(false);
            return;
          }
          if (result.active_job !== id) {
            activeJob = result.active_job;
            pollJob(activeJob);
            return;
          }
        } catch (_) { /* A failed read is not evidence that the model job ended. */ }
      }
      setBusy(true);
      showJob("正在重查 AI 工作状态", explain(error) + " 任务结果尚未确认；保持锁定并继续查询，不会重复调用模型。", "checking");
      pollTimer = window.setTimeout(() => pollJob(id), 3000);
    }
  }

  async function copyText(text) {
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error();
      await navigator.clipboard.writeText(text);
      toast("已复制纯正文。复制不改变发表状态。");
    } catch (_) { toast("浏览器未允许复制。请在稿件正文中选中文字后复制。", true); }
  }

  function collectProfiles() {
    const result = {};
    for (const [group, fields] of Object.entries(formIds)) {
      result[group] = {};
      for (const [key, id] of Object.entries(fields)) result[group][key] = $(id).value;
    }
    return result;
  }

  function materialCount() { $("material-count").textContent = $("material").value.length + " / 8000 字"; }

  document.querySelectorAll("[data-view]").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.view)));
  document.querySelectorAll("[data-go]").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.go)));
  document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); navigate("start"); });
  $("menu-toggle").addEventListener("click", () => {
    const expanded = $("sidebar").classList.toggle("open");
    $("menu-toggle").setAttribute("aria-expanded", String(expanded));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      $("sidebar").classList.remove("open");
      $("menu-toggle").setAttribute("aria-expanded", "false");
    }
  });
  $("refresh-button").addEventListener("click", async () => {
    $("refresh-button").disabled = true;
    try { await loadState(); toast("已重新读取云端工作区状态。"); }
    catch (error) { alertMessage(explain(error)); }
    finally { $("refresh-button").disabled = false; }
  });
  $("model-provider").addEventListener("change", async () => {
    if (busy || !state) return;
    const provider = $("model-provider").value;
    setBusy(true);
    try {
      await api("/api/model", { provider });
      await loadState();
      toast("已切换为“" + modelLabel() + "”。切换不调用模型。", !modelReady());
    } catch (error) {
      $("model-provider").value = "zhipu";
      toast(explain(error), true);
    } finally { setBusy(Boolean(activeJob) || submissionUncertain); }
  });
  $("job-dismiss").addEventListener("click", () => {
    if (!activeJob && !submissionUncertain) $("job-status").hidden = true;
  });
  $("job-result").addEventListener("click", () => {
    navigate("start");
    const variant = typeof completedJobTarget === "number" ? completedJobTarget : completedJobTarget && completedJobTarget.variant;
    const target = variant ? $("draft-grid").querySelector('[data-variant="' + Number(variant) + '"]') : $("round-heading");
    if (target && !target.hidden) target.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("profile-form").addEventListener("input", (event) => {
    profileDirty = true;
    if (event.target.removeAttribute) event.target.removeAttribute("aria-invalid");
    $("profile-save-hint").textContent = "有修改尚未保存，请点击保存说明书。";
  });
  $("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const payload = collectProfiles();
    const missing = Object.values(formIds).flatMap((fields) => Object.values(fields)).filter((id) => !$(id).value.trim());
    if (missing.length) {
      missing.forEach((id) => $(id).setAttribute("aria-invalid", "true"));
      toast("请填写必填项：" + missing.map((id) => requiredProfileLabels[id]).join("、") + "。", true);
      $(missing[0]).focus();
      return;
    }
    setBusy(true);
    try {
      await api("/api/profile", payload);
      profileDirty = false;
      await loadState({ forceProfile: true });
      toast("说明书已保存到云端并重新读取，未调用模型。");
    } catch (error) { toast(explain(error), true); }
    finally { setBusy(Boolean(activeJob) || submissionUncertain); }
  });
  $("history-file").addEventListener("change", async () => {
    const file = $("history-file").files && $("history-file").files[0];
    if (!file) return;
    try {
      if (!/\.(md|txt)$/i.test(file.name)) throw new Error("请选择.md或.txt文本文件。");
      if (file.size > 128 * 1024) throw new Error("文件超过128 KiB，请只导入本次需要的小篇作品。");
      const body = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      $("history-body").value = body;
      $("history-filename").value = file.name.replace(/\.(md|txt)$/i, ".md");
      loadedFile = { body, normalized: $("history-body").value };
      toast(/\.txt$/i.test(file.name) ? "文本已载入，将以.md副本保存；原文件不变。" : "作品已载入，确认后点击保存。");
    } catch (error) { loadedFile = null; toast(error instanceof TypeError ? "文件不是有效UTF-8文本，请先另存为UTF-8。" : explain(error), true); }
  });
  $("history-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const current = $("history-body").value;
    const body = loadedFile && current === loadedFile.normalized ? loadedFile.body : current;
    let filename = $("history-filename").value.trim();
    if (/\.txt$/i.test(filename)) filename = filename.replace(/\.txt$/i, ".md");
    else if (!/\.md$/i.test(filename)) filename += ".md";
    if (!body.trim()) { toast("先选择文件或粘贴正文。", true); return; }
    setBusy(true);
    try {
      await api("/api/history", { filename, body });
      $("history-body").value = "";
      $("history-file").value = "";
      loadedFile = null;
      await loadState();
      toast("历史原文已保存到云端；未推断发表状态，也未调用模型。");
    } catch (error) { toast(explain(error), true); }
    finally { setBusy(Boolean(activeJob) || submissionUncertain); }
  });
  $("close-history-reader").addEventListener("click", () => { $("history-reader").hidden = true; });
  $("use-history-button").addEventListener("click", () => {
    if (!historyReading) return;
    const addition = "参考旧作品（不自动代表本次经历或观点）：\n" + historyReading.body + "\n\n这一次我想表达：\n";
    if ($("material").value.length + addition.length > 8000) { toast("这篇加进来会超过8000字，请只选需要的片段。", true); return; }
    $("material").value += ($("material").value ? "\n\n" : "") + addition;
    materialCount();
    navigate("write");
    $("material").focus();
    toast("已加入本次材料，请补充这次想表达的观点。");
  });
  $("material").addEventListener("input", materialCount);
  $("generate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!$("material").value.trim()) { $("material").focus(); return; }
    if ($("material").value.trim().split(/\s+/).every((part) => /^https?:\/\/\S+$/i.test(part))) {
      toast("当前界面不会自动打开链接。请粘贴相关原文，并补充这次想表达的观点。", true);
      $("material").focus();
      return;
    }
    if (!modelReady()) { toast("写作服务暂不可用，请稍后刷新状态。", true); return; }
    const setup = onboardingState();
    if (!setup.profile_ready) { toast("先保存个人说明书的3项必填内容。", true); navigate("profile"); return; }
    selectedVariant = null;
    await startJob("/api/generate", { material: $("material").value }, "正在为这次材料寻找三个切口");
  });
  $("materials-filter").addEventListener("change", renderHistory);
  const starters = {
    daily: "今天发生的一件小事：\n\n我的感受和想法：\n\n想让读者记住：\n",
    discovery: "今天工作中的一个发现：\n\n我观察到的具体细节：\n\n我的判断与还不确定的地方：\n",
    review: "最近遇到的问题：\n\n我尝试了什么：\n\n这次得到的提醒：\n"
  };
  document.querySelectorAll("[data-starter]").forEach((item) => item.addEventListener("click", () => {
    const addition = starters[item.dataset.starter];
    if (!addition) return;
    const current = $("material").value;
    const combined = current + (current.trim() ? "\n\n" : "") + addition;
    if (combined.length > 8000) { toast("材料将超过8000字，请先整理已有内容。", true); return; }
    $("material").value = combined;
    materialCount();
    $("material").focus();
  }));
  $("library-search").addEventListener("input", renderLibrary);
  $("library-filter").addEventListener("change", renderLibrary);

  try { activeView = window.sessionStorage.getItem(viewKey) || "start"; } catch (_) { /* Default home. */ }
  navigate(activeView, false);
  updateButtons();
  loadState().catch((error) => {
    $("connection-status").className = "status-label error";
    $("connection-label").textContent = "云端连接未完成";
    $("home-model-label").textContent = "连接后查看写作状态";
    $("home-model-message").textContent = "请检查网络，稍后刷新以恢复当前浏览器的工作区。";
    alertMessage(explain(error));
  });
})();
