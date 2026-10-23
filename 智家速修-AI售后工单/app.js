// app.js
App({
  globalData: {
    role: 'user',  // user(报修人) / engineer(工程师) / support(客服)
    user: { name: '张住户', phone: '138****6688', device: '智能门锁 Pro' }
  },
  onLaunch() {
    const saved = wx.getStorageSync('zj_role');
    if (saved) this.globalData.role = saved;
  },
  setRole(r) {
    this.globalData.role = r;
    wx.setStorageSync('zj_role', r);
  }
});
