const API_BASE = "/mba/booking/api";

let currentUser = {};
let scheduleConfig = [];
let bookings = [];
let systemCounts = { students: 0, supervisors: 0 };

const firstNameInput = document.getElementById("firstName");
const surnameInput = document.getElementById("surname");
const emailInput = document.getElementById("email");
const roleSelect = document.getElementById("role");
const supervisorFieldContainer = document.getElementById("supervisorFieldContainer");
const supervisorName = document.getElementById("supervisorName");
const supervisorId = document.getElementById("supervisorId");
const coSupervisorFieldContainer = document.getElementById("coSupervisorFieldContainer");
const coSupervisorSearch = document.getElementById("coSupervisorSearch");
const coSupervisorId = document.getElementById("coSupervisorId");
const supervisorDropdown = document.getElementById("supervisorDropdown");
const dateSelect = document.getElementById("date");
const panelSelect = document.getElementById("panel");
const slotSelect = document.getElementById("slot");
const slotLabel = document.getElementById("slotLabel");
const messageBox = document.getElementById("message");
const bookButton = document.getElementById("bookButton");
const releaseState = document.getElementById("releaseState");

async function apiFetch(path, options) {
  const response = await fetch(API_BASE + path, options || {});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    throw new Error(payload.message || "Request failed.");
  }
  return payload;
}

function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
}

function debounce(func, delay) {
  let timeoutId;
  return function () {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(func, delay);
  };
}

function showMessage(text, type) {
  messageBox.textContent = text;
  messageBox.className = "booking-message " + (type || "");
}

function getSelectedDay() {
  return scheduleConfig.find(function (day) {
    return day.date === dateSelect.value;
  }) || scheduleConfig[0];
}

function roleForBooking() {
  return currentUser.role || roleSelect.value || "student";
}

function fillUserFields() {
  firstNameInput.value = currentUser.firstName || "";
  surnameInput.value = currentUser.surname || "";
  emailInput.value = currentUser.email || "";
  roleSelect.value = currentUser.role || "student";
  supervisorName.value = currentUser.supervisorName || "";
  supervisorId.value = currentUser.supervisorId || "";

  const isStudent = roleForBooking() === "student";
  supervisorFieldContainer.style.display = isStudent ? "block" : "none";
  coSupervisorFieldContainer.style.display = isStudent ? "block" : "none";

  if (releaseState) {
    releaseState.textContent = currentUser.isReleased ? "Booking page is released." : "Booking page is locked for students and supervisors.";
  }
  if (!currentUser.canBook && !currentUser.isAdmin) {
    bookButton.disabled = true;
    showMessage("Your MBA role cannot create panel bookings.", "info");
  } else if (!currentUser.isReleased && !currentUser.isAdmin) {
    bookButton.disabled = true;
    showMessage("Panel booking is locked until MBA Admin releases the page.", "info");
  }
}

async function refreshData() {
  const data = await Promise.all([
    apiFetch("/me"),
    apiFetch("/schedule"),
    apiFetch("/bookings"),
    apiFetch("/system-counts"),
  ]);
  currentUser = data[0];
  scheduleConfig = data[1];
  bookings = data[2];
  systemCounts = data[3];
  fillUserFields();
  renderSystemCounts();
}

function renderSystemCounts() {
  document.getElementById("systemStudents").textContent = systemCounts.students;
  document.getElementById("systemSupervisors").textContent = systemCounts.supervisors;
}

function loadDates() {
  dateSelect.innerHTML = "";
  scheduleConfig.forEach(function (day) {
    const option = document.createElement("option");
    option.value = day.date;
    option.textContent = day.displayDate;
    dateSelect.appendChild(option);
  });
}

function loadPanels() {
  const day = getSelectedDay();
  panelSelect.innerHTML = "";
  if (!day) return;
  day.panels.forEach(function (panel) {
    const option = document.createElement("option");
    option.value = panel;
    const hasConflict = roleForBooking() === "supervisor"
      ? panelHasOwnStudentConflict(day.date, panel)
      : panelHasOwnSupervisorConflict(day.date, panel);
    option.textContent = panel + (hasConflict ? " - Conflict" : "");
    panelSelect.appendChild(option);
  });
}

function slotIsTaken(date, panel, role, slot) {
  return bookings.some(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === role && booking.slot === slot;
  });
}

function panelHasOwnStudentConflict(date, panel) {
  if (roleForBooking() !== "supervisor") return false;
  return bookings.some(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === "student" && booking.supervisorId === currentUser.id;
  });
}

function panelHasOwnSupervisorConflict(date, panel) {
  if (roleForBooking() !== "student") return false;
  return bookings.some(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === "supervisor" && booking.userId === currentUser.supervisorId;
  });
}

function supervisorAlreadyOnPanel(date, panel) {
  if (roleForBooking() !== "supervisor") return false;
  return bookings.some(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === "supervisor" && booking.userId === currentUser.id;
  });
}

function loadSlots() {
  const day = getSelectedDay();
  slotSelect.innerHTML = "";
  if (!day) return;

  const role = roleForBooking();
  const panel = panelSelect.value;
  const slots = role === "student" ? day.studentSlots : day.supervisorSlots;
  const panelConflict = role === "supervisor" ? panelHasOwnStudentConflict(day.date, panel) : panelHasOwnSupervisorConflict(day.date, panel);
  const samePanelBooked = supervisorAlreadyOnPanel(day.date, panel);

  slotLabel.textContent = role === "student" ? "Time" : "Supervisor Slot";
  const hasPinnedMessage = messageBox.className.includes("success") || messageBox.className.includes("error");
  if (panelConflict && !hasPinnedMessage) {
    showMessage(
      role === "supervisor"
        ? "You cannot book this panel because one of your students is already booked here."
        : "You cannot book this panel because your supervisor is already booked here.",
      "info"
    );
  } else if (messageBox.className.includes("info")) {
    showMessage("", "");
  }
  slots.forEach(function (slot) {
    const option = document.createElement("option");
    option.value = slot;
    const taken = slotIsTaken(day.date, panel, role, slot);
    option.disabled = taken || panelConflict || samePanelBooked;
    option.textContent = slot + (taken ? " - Taken" : panelConflict ? " - Conflict" : samePanelBooked ? " - Already booked" : "");
    slotSelect.appendChild(option);
  });
}

async function handleSupervisorSearch() {
  const query = coSupervisorSearch.value.trim();
  coSupervisorId.value = "";
  if (query.length < 2) {
    supervisorDropdown.innerHTML = "";
    supervisorDropdown.style.display = "none";
    return;
  }

  try {
    const data = await apiFetch("/supervisors/search?q=" + encodeURIComponent(query));
    const results = data.results || [];
    supervisorDropdown.innerHTML = results.length ? results.map(function (supervisor) {
      return "<button type='button' class='booking-button subtle' style='width:100%;margin-top:.25rem' data-id='" + supervisor.id + "' data-name='" + supervisor.name.replace(/'/g, "&#39;") + "'>" + supervisor.name + " (" + supervisor.email + ")</button>";
    }).join("") : "<div class='booking-message info'>No supervisors found.</div>";
    supervisorDropdown.style.display = "block";
  } catch (error) {
    supervisorDropdown.innerHTML = "<div class='booking-message error'>Error loading supervisors.</div>";
    supervisorDropdown.style.display = "block";
  }
}

async function bookSlot() {
  const day = getSelectedDay();
  if (!day) {
    showMessage("MBA Admin must create booking dates first.", "error");
    return;
  }

  const payload = {
    role: roleForBooking(),
    date: day.date,
    panel: panelSelect.value,
    slot: slotSelect.value,
    coSupervisorId: coSupervisorId.value,
    coSupervisorName: coSupervisorSearch.value.trim(),
  };

  try {
    await apiFetch("/bookings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
      },
      body: JSON.stringify(payload),
    });
    await refreshData();
    coSupervisorSearch.value = "";
    coSupervisorId.value = "";
    showMessage("Booking confirmed.", "success");
    loadDates();
    loadPanels();
    loadSlots();
    renderSchedule();
  } catch (error) {
    showMessage(error.message || "Booking failed.", "error");
    loadSlots();
  }
}

function getBooking(date, panel, role, slot) {
  return bookings.find(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === role && booking.slot === slot;
  });
}

function countBookings(date, panel, role) {
  return bookings.filter(function (booking) {
    return booking.date === date && booking.panel === panel && booking.role === role;
  }).length;
}

function renderSchedule() {
  const scheduleDiv = document.getElementById("schedule");
  scheduleDiv.innerHTML = "";

  if (!scheduleConfig.length) {
    scheduleDiv.innerHTML = "<div class='booking-message info'>No booking dates have been created yet.</div>";
    document.getElementById("totalStudents").textContent = "0/0";
    document.getElementById("totalSupervisors").textContent = "0/0";
    return;
  }

  let totalStudents = 0;
  let totalSupervisors = 0;
  scheduleConfig.forEach(function (day) {
    const daySection = document.createElement("div");
    daySection.className = "day-section";
    daySection.innerHTML = "<div class='day-title'><span>" + day.displayDate + "</span><span>" + day.panels.length + " panels</span></div>";
    const panelGrid = document.createElement("div");
    panelGrid.className = "panel-grid";

    day.panels.forEach(function (panel) {
      const studentCount = countBookings(day.date, panel, "student");
      const supervisorCount = countBookings(day.date, panel, "supervisor");
      totalStudents += studentCount;
      totalSupervisors += supervisorCount;
      const isFull = studentCount === day.studentSlots.length && supervisorCount === day.supervisorSlots.length;
      const hasConflict = roleForBooking() === "supervisor"
        ? panelHasOwnStudentConflict(day.date, panel)
        : panelHasOwnSupervisorConflict(day.date, panel);
      const panelCard = document.createElement("div");
      panelCard.className = isFull ? "panel-card full" : hasConflict ? "panel-card conflict" : "panel-card";

      let studentRows = "";
      day.studentSlots.forEach(function (slot) {
        const booking = getBooking(day.date, panel, "student", slot);
        studentRows += "<div class='slot-row'><strong>" + slot + "</strong><br>" + (booking ? booking.name : "<span class='empty'>Open</span>") + "</div>";
      });

      let supervisorRows = "";
      day.supervisorSlots.forEach(function (slot) {
        const booking = getBooking(day.date, panel, "supervisor", slot);
        supervisorRows += "<div class='slot-row'><strong>" + slot + "</strong><br>" + (booking ? booking.name : "<span class='empty'>Open</span>") + "</div>";
      });

      panelCard.innerHTML =
        "<div class='panel-top'><h3>" + panel + "</h3><span class='badge " + (isFull ? "full-badge" : hasConflict ? "conflict-badge" : "open-badge") + "'>" + (isFull ? "Full" : hasConflict ? "Conflict" : "Open") + "</span></div>" +
        "<div class='counts'><div class='count-box'><strong>" + studentCount + "/" + day.studentSlots.length + "</strong>Students</div><div class='count-box'><strong>" + supervisorCount + "/" + day.supervisorSlots.length + "</strong>Supervisors</div></div>" +
        "<div class='list'><div class='list-title'>Students</div>" + studentRows + "<div class='list-title'>Supervisors</div>" + supervisorRows + "</div>";
      panelGrid.appendChild(panelCard);
    });

    daySection.appendChild(panelGrid);
    scheduleDiv.appendChild(daySection);
  });

  document.getElementById("totalStudents").textContent = totalStudents + "/" + Math.max(systemCounts.students, totalStudents);
  document.getElementById("totalSupervisors").textContent = totalSupervisors + "/" + Math.max(systemCounts.supervisors, totalSupervisors);
}

async function startApp() {
  try {
    await refreshData();
    loadDates();
    loadPanels();
    loadSlots();
    renderSchedule();
  } catch (error) {
    showMessage(error.message || "Failed to load booking data.", "error");
  }

  dateSelect.addEventListener("change", function () {
    loadPanels();
    loadSlots();
  });
  panelSelect.addEventListener("change", loadSlots);
  bookButton.addEventListener("click", bookSlot);
  coSupervisorSearch.addEventListener("input", debounce(handleSupervisorSearch, 250));
  supervisorDropdown.addEventListener("click", function (event) {
    const target = event.target.closest("button[data-id]");
    if (!target) return;
    coSupervisorId.value = target.dataset.id || "";
    coSupervisorSearch.value = target.dataset.name || "";
    supervisorDropdown.innerHTML = "";
    supervisorDropdown.style.display = "none";
  });
}

document.addEventListener("DOMContentLoaded", startApp);
