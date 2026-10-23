// app.js
App({
  globalData: {
    // 当前身份：employee(小李) / manager(王经理) / finance(陈姐)
    role: 'employee',
    user: {
      empNo: 'LI-2024',
      name: '李小禾',
      dept: '市场部'
    }
  },
  onLaunch() {
    const saved = wx.getStorageSync('role');
    if (saved) this.globalData.role = saved;
  },
  setRole(r) {
    this.globalData.role = r;
    wx.setStorageSync('role', r);
  }
});
