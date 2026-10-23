// pages/bills/bills.js
const store = require('../../utils/store.js');
const dict = require('../../utils/dict.js');

Page({
  data: { list: [], filter: 'all' },
  onShow() {
    let list = store.listMyBills();
    if (this.data.filter !== 'all') {
      list = list.filter(b => b.status === this.data.filter);
    }
    this.setData({ list });
  },
  setFilter(e) {
    this.setData({ filter: e.currentTarget.dataset.f });
    this.onShow();
  },
  openDetail(e) {
    const bill = this.data.list[e.currentTarget.dataset.idx];
    let timeline = '流转时间轴：\n';
    bill.timeline.forEach(t => { timeline += `· ${t.t}  ${t.who}：${t.action}\n`; });
    wx.showModal({ title: bill.id, content: timeline, showCancel: false });
  }
});
