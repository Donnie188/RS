// pages/me/me.js
const app = getApp();
const dict = require('../../utils/dict.js');

Page({
  data: { role: 'employee', user: {} },
  onShow() {
    const role = app.globalData.role;
    this.setData({ role, user: dict.roles[role] });
  },
  switchRole(e) {
    const r = e.currentTarget.dataset.r;
    app.setRole(r);
    this.setData({ role: r, user: dict.roles[r] });
    wx.showToast({ title: '已切换：' + dict.roles[r].label, icon: 'none' });
  }
});
