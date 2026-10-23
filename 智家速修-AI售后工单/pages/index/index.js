// pages/index/index.js
const app = getApp();
const store = require('../../utils/store.js');
const dict = require('../../utils/dict.js');

Page({
  data: { role: 'user', stats: { total:0, doing:0, done:0 }, recent: [] },
  onShow() {
    const role = app.globalData.role;
    const all = store.listAll();
    this.setData({
      role,
      stats: {
        total: all.length,
        doing: all.filter(o => ['submitted','diagnosing','dispatched','repairing'].includes(o.status)).length,
        done: all.filter(o => o.status === 'done').length
      },
      recent: all.slice(0, 3)
    });
  },
  goReport() { wx.switchTab({ url: '/pages/report/report' }); },
  goOrders() { wx.switchTab({ url: '/pages/orders/orders' }); }
});
