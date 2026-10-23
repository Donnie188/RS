// pages/approval/approval.js
const app = getApp();
const store = require('../../utils/store.js');
const dict = require('../../utils/dict.js');

Page({
  data: { role: 'employee', list: [], roleName: '' },
  onShow() {
    const role = app.globalData.role;
    const list = store.listPending(role);
    this.setData({
      role,
      list,
      roleName: dict.roles[role].label
    });
  },
  approve(e) {
    const { id, idx } = e.currentTarget.dataset;
    const who = dict.roles[this.data.role].name;
    const next = this.data.role === 'manager' ? 'auditing' : 'paid';
    store.approve(id, who, this.data.role === 'manager' ? '主管审批通过' : '财务复核通过，已打款', next);
    wx.showToast({ title: '已通过', icon: 'success' });
    this.onShow();
  },
  reject(e) {
    const { id } = e.currentTarget.dataset;
    const who = dict.roles[this.data.role].name;
    store.approve(id, who, '驳回：票据信息不完整', 'rejected');
    wx.showToast({ title: '已驳回', icon: 'none' });
    this.onShow();
  }
});
