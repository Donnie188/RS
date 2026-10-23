// utils/store.js — 数据层：本地存储 + 权限规则
const seed = require('./seed.js');
const dict = require('./dict.js');

const KEY = 'qinghe_bills_v2';

function load() {
  let data = wx.getStorageSync(KEY);
  if (!data || !Array.isArray(data)) {
    data = seed.bills;
    wx.setStorageSync(KEY, data);
  }
  return data;
}
function save(bills) {
  wx.setStorageSync(KEY, bills);
}

module.exports = {
  // 员工看自己的单据
  listMyBills() {
    return load();
  },
  // 主管/财务看全部待办
  listPending(role) {
    const all = load();
    if (role === 'manager') return all.filter(b => b.status === 'submitted' || b.status === 'reviewing');
    if (role === 'finance') return all.filter(b => b.status === 'auditing' || b.status === 'reviewing');
    return all;
  },
  // 新建单据
  createBill(bill) {
    const all = load();
    bill.id = 'BX-' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '-' + String(all.length + 1).padStart(3, '0');
    bill.status = 'submitted';
    bill.timeline = [{ t: new Date().toLocaleString('zh-CN'), who: '李小禾', action: '提交报销单' }];
    all.unshift(bill);
    save(all);
    return bill;
  },
  // 审批操作
  approve(id, who, action, nextStatus) {
    const all = load();
    const idx = all.findIndex(b => b.id === id);
    if (idx < 0) return null;
    all[idx].status = nextStatus;
    all[idx].timeline.push({ t: new Date().toLocaleString('zh-CN'), who, action });
    save(all);
    return all[idx];
  }
};
