// pages/index/index.js
const app = getApp();
const store = require('../../utils/store.js');
const dict = require('../../utils/dict.js');

Page({
  data: {
    role: 'employee',
    user: {},
    stats: { total: 0, pending: 0, paid: 0, monthAmount: 0 },
    recent: []
  },
  onShow() {
    const role = app.globalData.role;
    const bills = store.listMyBills();
    const paid = bills.filter(b => b.status === 'paid');
    const pending = bills.filter(b => ['submitted','reviewing','auditing'].includes(b.status));
    this.setData({
      role,
      user: dict.roles[role],
      stats: {
        total: bills.length,
        pending: pending.length,
        paid: paid.length,
        monthAmount: paid.reduce((s,b) => s + b.amount, 0).toFixed(2)
      },
      recent: bills.slice(0, 3)
    });
  },
  goCapture() { wx.switchTab({ url: '/pages/capture/capture' }); },
  goBills() { wx.switchTab({ url: '/pages/bills/bills' }); },
  goApproval() { wx.switchTab({ url: '/pages/approval/approval' }); }
});
