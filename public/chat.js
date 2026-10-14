// Chat client for the minihttp WebSocket server. Messages are rendered with
// textContent only, so nothing a user types can inject HTML or scripts.

const log = document.getElementById("log");
const status = document.getElementById("status");
const joinForm = document.getElementById("join");
const sendForm = document.getElementById("send");
let socket = null;
let retryDelay = 1000;
let wanted = null; // {name, room} while the user wants to be connected

function line(className) {
  const li = document.createElement("li");
  if (className) li.className = className;
  log.append(li);
  log.scrollTop = log.scrollHeight;
  return li;
}

function showMessage(m) {
  const li = line();
  const time = document.createElement("time");
  time.textContent = new Date(m.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const who = document.createElement("strong");
  who.textContent = `${m.name}: `;
  li.append(time, who, document.createTextNode(m.text));
}

function connect() {
  const { name, room } = wanted;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ name, room });
  socket = new WebSocket(`${scheme}://${location.host}/ws/chat?${params}`);
  status.textContent = "· connecting…";

  socket.onopen = () => {
    retryDelay = 1000;
    status.textContent = `· #${room} as ${name}`;
    sendForm.hidden = false;
    sendForm.text.focus();
  };
  socket.onmessage = (event) => {
    const m = JSON.parse(event.data);
    if (m.type === "history") {
      log.replaceChildren();
      m.messages.forEach(showMessage);
      line("notice").textContent = `Joined #${m.room}`;
    } else if (m.type === "message") {
      showMessage(m);
    } else if (m.type === "join" || m.type === "leave") {
      line("notice").textContent = `${m.name} ${m.type === "join" ? "joined" : "left"} · ${m.users} online`;
    } else if (m.type === "error") {
      line("error").textContent = m.error;
    }
  };
  socket.onclose = (event) => {
    sendForm.hidden = true;
    if (event.code === 1008) {  // policy violation: invalid name/room, don't retry
      status.textContent = `· ${event.reason || "rejected"}`;
      wanted = null;
      return;
    }
    if (!wanted) return;
    status.textContent = `· disconnected, retrying in ${retryDelay / 1000}s`;
    setTimeout(() => wanted && connect(), retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000); // exponential backoff
  };
}

joinForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (socket) {
    wanted = null;
    socket.close(1000, "switching room");
  }
  wanted = { name: joinForm.name.value.trim(), room: joinForm.room.value.trim().toLowerCase() };
  connect();
});

sendForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (socket?.readyState === WebSocket.OPEN && sendForm.text.value.trim()) {
    socket.send(JSON.stringify({ text: sendForm.text.value }));
    sendForm.text.value = "";
  }
});
