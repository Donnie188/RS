// utils/store.js
const seed = require('./seed.js');
const dict = require('./dict.js');

const KEY = 'zhijia_orders_v1';

function load() {
  let data = wx.getStorageSync(KEY);
  if (!data || !Array.isArray(data)) {
    data = seed.orders;
    wx.setStorageSync(KEY, data);
  }
  return data;
}
function save(list) { wx.setStorageSync(KEY, list); }

module.exports = {
  listAll() { return load(); },
  listByStatus(status) { return load().filter(o => o.status === status); },
  createOrder(order) {
    const all = load();
    order.id = 'GD-' + new Date().toISOString().slice(0,10).replace(/-/g,'') + '-' + String(all.length+1).padStart(3,'0');
    order.status = 'diagnosing';
    order.timeline = [{ t: new Date().toLocaleString('zh-CN'), who: '张住户', action: '提交报修' }];
    all.unshift(order);
    save(all);
    return order;
  },
  updateOrder(id, patch) {
    const all = load();
    const idx = all.findIndex(o => o.id === id);
    if (idx < 0) return null;
    Object.assign(all[idx], patch);
    save(all);
    return all[idx];
  },
  addTimeline(id, who, action) {
    const all = load();
    const idx = all.findIndex(o => o.id === id);
    if (idx < 0) return;
    all[idx].timeline.push({ t: new Date().toLocaleString('zh-CN'), who, action });
    save(all);
  }
};
