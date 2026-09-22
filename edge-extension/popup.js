const labels = {IDLE: "空闲", RUNNING: "演练中", OPENED: "商品已打开", CHECKOUT_READY: "Checkout 已核实 · 已停止", WAITING_FOR_HUMAN: "等待本人操作", PAUSED_DISCONNECTED: "连接中断 · 已暂停", PAUSED_RESTART: "重启后暂停", PAUSED_UNCERTAIN: "结果待核实 · 已暂停", DISARMED: "已停止", CANCELLED: "意图已停止", FAILED: "已停止，请查看 Core 状态"};
const show = (id, value) => { document.getElementById(id).textContent = value; };
async function refresh(action = "STATUS") {
  try {
    const state = await chrome.runtime.sendMessage({action});
    if (!state) throw new Error("NO_STATUS");
    show("installed", state.installed ? "已安装" : "未知"); show("connected", state.connected ? "已连接" : "未连接");
    show("version", state.version); show("heartbeat", state.last_heartbeat ? new Date(state.last_heartbeat).toLocaleTimeString() : "尚无");
    show("tab", state.tab_id === null ? "尚无" : String(state.tab_id)); show("intent", state.intent_id || "尚无");
    show("state", labels[state.state] || state.state); show("login", ({VALID:"已核实", REQUIRED:"需要本人登录", UNKNOWN:"尚未核实"})[state.login] || "尚未核实"); show("challenge", ({NONE: "当前未发现", REQUIRED: "等待本人验证", UNKNOWN: "尚未核实"})[state.challenge] || "尚未核实");
    show("notice", state.mutation_uncertain ? "已有操作发出，结果尚未核实。不会自动重复操作。" : state.error ? `连接或页面状态：${state.error}` : "订单与付款均未开放。");
  } catch { show("connected", "不可用"); show("notice", "请先运行本机安装向导，然后点击连接。"); }
}
document.getElementById("connect").addEventListener("click", () => { void refresh("CONNECT"); });
void refresh(); setInterval(() => { void refresh(); }, 2000);
