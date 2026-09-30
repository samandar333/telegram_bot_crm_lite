const telegram = window.Telegram?.WebApp;
const $ = (selector) => document.querySelector(selector);
const form = $("#report-form");
const mediaInput = $("#media-input");
const mediaPreview = $("#media-preview");
const fileName = $(".upload-copy strong");
const fileHint = $(".upload-copy small");
const submitButton = $("#submit-button");
const formMessage = $("#form-message");
const reportList = $("#report-list");
const views = { new: $("#view-new"), active: $("#view-active") };
const tabs = [...document.querySelectorAll(".tab")];

const copy = {
  uz: {
    brand: "P24TEX · TEXNIK XIZMAT", title: "Nosozliklar", tabNew: "Yangi report", tabActive: "Faol",
    newEyebrow: "YANGI MUROJAAT", newTitle: "Nosozlik haqida xabar bering", objectLabel: "Obyekt",
    issueLabel: "Nosozlik turi", mediaLabel: "Foto yoki video", mediaHint: "Vaziyatni aniq ko'rsatadigan fayl",
    required: "KERAK", uploadTitle: "Fayl tanlang", uploadHint: "Rasm yoki video · 20 MB gacha",
    descriptionLabel: "Izoh", optional: "ixtiyoriy", descriptionPlaceholder: "Qo'shimcha ma'lumot...",
    descriptionHint: "Joylashuv yoki tafsilotlarni yozing", submit: "Report yuborish", activeEyebrow: "NAZORATDA",
    activeTitle: "Faol reportlar", footer: "P24TEX TEXNIK XIZMAT", emptyObjects: "Sizga obyekt biriktirilmagan.",
    chooseObject: "Obyektni tanlang", chooseIssue: "Nosozlik turini tanlang", chooseMedia: "Foto yoki video tanlang.",
    badMedia: "Rasm yoki video faylini tanlang.", mediaReady: "Fayl tanlandi", sending: "Yuborilmoqda...",
    sent: "Report guruhga yuborildi.", openTelegram: "Mini App’ni bot ichidan oching.", loadError: "Ma'lumotlarni yuklab bo'lmadi. Qayta urinib ko'ring.",
    sendError: "Report yuborilmadi. Internetni tekshirib, qayta urinib ko'ring.", noReports: "Hozircha faol reportlar yo'q.",
    report: "Report", waiting: "Kutilmoqda", refresh: "Yangilash", retry: "Qayta urinib ko'ring",
    issueLabels: ["🚧 Shlagbaum", "📹 Kamera", "💳 To'lov terminali", "🌐 Server / tarmoq"],
  },
  ru: {
    brand: "P24TEX · ТЕХНИЧЕСКАЯ СЛУЖБА", title: "Неисправности", tabNew: "Новый отчет", tabActive: "Активные",
    newEyebrow: "НОВОЕ ОБРАЩЕНИЕ", newTitle: "Сообщить о неисправности", objectLabel: "Объект",
    issueLabel: "Тип неисправности", mediaLabel: "Фото или видео", mediaHint: "Файл, наглядно показывающий проблему",
    required: "ОБЯЗАТЕЛЬНО", uploadTitle: "Выбрать файл", uploadHint: "Фото или видео · до 20 МБ",
    descriptionLabel: "Комментарий", optional: "необязательно", descriptionPlaceholder: "Дополнительная информация...",
    descriptionHint: "Укажите расположение или детали", submit: "Отправить отчет", activeEyebrow: "НА КОНТРОЛЕ",
    activeTitle: "Активные отчеты", footer: "P24TEX ТЕХНИЧЕСКАЯ СЛУЖБА", emptyObjects: "Вам не назначены объекты.",
    chooseObject: "Выберите объект", chooseIssue: "Выберите тип неисправности", chooseMedia: "Выберите фото или видео.",
    badMedia: "Выберите файл фото или видео.", mediaReady: "Файл выбран", sending: "Отправка...",
    sent: "Отчет отправлен в группу.", openTelegram: "Откройте Mini App из бота.", loadError: "Не удалось загрузить данные. Попробуйте еще раз.",
    sendError: "Не удалось отправить отчет. Проверьте интернет и повторите попытку.", noReports: "Активных отчетов пока нет.",
    report: "Отчет", waiting: "Ожидает", refresh: "Обновить", retry: "Повторить",
    issueLabels: ["🚧 Шлагбаум", "📹 Камера", "💳 Терминал оплаты", "🌐 Сервер / сеть"],
  },
};

let language = "uz";
let bootstrap = null;
let previewUrl = null;
let toastTimer = null;

function t(key) {
  return copy[language]?.[key] ?? copy.uz[key] ?? key;
}

function setLanguage(lang) {
  language = lang === "ru" ? "ru" : "uz";
  document.documentElement.lang = language;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const value = t(node.dataset.i18n);
    if (value !== undefined) node.textContent = value;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  $("#refresh-reports").title = t("refresh");
  $("#refresh-reports").setAttribute("aria-label", t("refresh"));
  if (bootstrap) {
    renderIssues();
    renderReports(bootstrap.reports);
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
}

function showFormMessage(message, error = false) {
  formMessage.textContent = message;
  formMessage.classList.toggle("is-error", error);
  formMessage.hidden = !message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `tma ${telegram?.initData ?? ""}`,
      ...(options.headers ?? {}),
    },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) throw new Error(t("openTelegram"));
    throw new Error(result.error || t("loadError"));
  }
  return result;
}

function renderObjects(objects) {
  const select = $("#object-select");
  select.replaceChildren();
  const prompt = document.createElement("option");
  prompt.value = "";
  prompt.textContent = t("chooseObject");
  prompt.disabled = true;
  prompt.selected = true;
  select.append(prompt);
  objects.forEach((object) => {
    const option = document.createElement("option");
    option.value = object.id;
    option.textContent = object.name;
    select.append(option);
  });
  select.disabled = objects.length === 0;
  if (objects.length === 0) showFormMessage(t("emptyObjects"), true);
}

function renderIssues() {
  const grid = $("#issue-grid");
  grid.replaceChildren();
  if (!bootstrap) return;
  bootstrap.issue_types.forEach((issue, index) => {
    const label = document.createElement("label");
    label.className = "issue-option";
    label.innerHTML = `<span class="issue-icon" aria-hidden="true">${["🚧", "📹", "💳", "🌐"][index] ?? "⚠️"}</span>`;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "issue_type";
    input.value = issue.value;
    input.required = true;
    const text = document.createElement("span");
    text.textContent = issue.label;
    label.append(input, text);
    input.addEventListener("change", () => {
      grid.querySelectorAll(".issue-option").forEach((item) => item.classList.remove("is-selected"));
      label.classList.add("is-selected");
    });
    grid.append(label);
  });
}

function renderReports(reports = []) {
  reportList.replaceChildren();
  $("#active-count").textContent = String(reports.length);
  if (!reports.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `<span class="empty-mark" aria-hidden="true">✓</span>${t("noReports")}`;
    reportList.append(empty);
    return;
  }
  reports.forEach((report) => {
    const row = document.createElement("article");
    row.className = "report-row";
    const details = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = `${report.object_name} · ${report.issue_type}`;
    const meta = document.createElement("p");
    meta.textContent = `${t("report")} #${report.id} · ${formatDate(report.created_at)}`;
    details.append(title, meta);
    const status = document.createElement("span");
    status.className = "report-state";
    status.textContent = t("waiting");
    row.append(details, status);
    reportList.append(row);
  });
}

function formatDate(value) {
  const date = new Date(value.replace(" ", "T") + (value.endsWith("Z") ? "" : "Z"));
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "uz-UZ", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function selectView(view) {
  Object.entries(views).forEach(([key, element]) => { element.hidden = key !== view; });
  tabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === view));
  if (view === "active") refreshReports();
}

async function refreshReports() {
  try {
    const result = await api("/api/bootstrap");
    bootstrap.reports = result.reports;
    renderReports(result.reports);
  } catch (error) {
    showToast(error.message || t("loadError"));
  }
}

mediaInput.addEventListener("change", () => {
  const file = mediaInput.files?.[0];
  if (!file) return;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  fileName.textContent = file.name;
  fileHint.textContent = `${t("mediaReady")} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
  $("#upload-zone").classList.add("is-ready");
  mediaPreview.replaceChildren();
  mediaPreview.hidden = false;
  const preview = document.createElement(file.type.startsWith("video/") ? "video" : "img");
  preview.src = previewUrl;
  if (preview instanceof HTMLVideoElement) preview.controls = true;
  preview.alt = file.name;
  mediaPreview.append(preview);
});

$("#description").addEventListener("input", (event) => {
  $("#char-count").textContent = `${event.target.value.length} / 500`;
});

tabs.forEach((tab) => tab.addEventListener("click", () => selectView(tab.dataset.view)));
$("#refresh-reports").addEventListener("click", refreshReports);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showFormMessage("");
  const selectedIssue = form.querySelector('input[name="issue_type"]:checked');
  const media = mediaInput.files?.[0];
  if (!$("#object-select").value) return showFormMessage(t("chooseObject"), true);
  if (!selectedIssue) return showFormMessage(t("chooseIssue"), true);
  if (!media) return showFormMessage(t("chooseMedia"), true);
  if (!media.type.startsWith("image/") && !media.type.startsWith("video/")) return showFormMessage(t("badMedia"), true);
  if (media.size > 20 * 1024 * 1024) return showFormMessage(t("uploadHint"), true);

  const data = new FormData();
  data.set("object_id", $("#object-select").value);
  data.set("issue_type", selectedIssue.value);
  data.set("description", $("#description").value);
  data.set("media", media, media.name);
  submitButton.disabled = true;
  submitButton.querySelector("span:first-child").textContent = t("sending");
  try {
    const result = await api("/api/reports", { method: "POST", body: data });
    showFormMessage(`${t("sent")} #${result.report_id}`);
    form.reset();
    $("#char-count").textContent = "0 / 500";
    $("#upload-zone").classList.remove("is-ready");
    fileName.textContent = t("uploadTitle");
    fileHint.textContent = t("uploadHint");
    mediaPreview.replaceChildren();
    mediaPreview.hidden = true;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    form.querySelectorAll(".issue-option").forEach((item) => item.classList.remove("is-selected"));
    bootstrap.reports.unshift(result.report);
    renderReports(bootstrap.reports);
    telegram?.HapticFeedback?.notificationOccurred("success");
  } catch (error) {
    showFormMessage(error.message || t("sendError"), true);
    telegram?.HapticFeedback?.notificationOccurred("error");
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span:first-child").textContent = t("submit");
  }
});

async function start() {
  telegram?.ready();
  telegram?.expand();
  if (!telegram?.initData) {
    showFormMessage(t("openTelegram"), true);
    form.querySelectorAll("input, select, textarea, button").forEach((input) => { input.disabled = true; });
    return;
  }
  try {
    bootstrap = await api("/api/bootstrap");
    setLanguage(bootstrap.language);
    const name = bootstrap.full_name || "P";
    $("#user-mark").textContent = name.trim().slice(0, 1).toLocaleUpperCase();
    renderObjects(bootstrap.objects);
    renderIssues();
    renderReports(bootstrap.reports);
  } catch (error) {
    showFormMessage(error.message || t("loadError"), true);
    form.querySelectorAll("input, select, textarea, button").forEach((input) => { input.disabled = true; });
  }
}

start();
