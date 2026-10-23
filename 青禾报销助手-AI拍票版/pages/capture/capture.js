// pages/capture/capture.js — 拍票识别 + 自动填单
const ocr = require('../../utils/ocr.js');
const store = require('../../utils/store.js');
const dict = require('../../utils/dict.js');

Page({
  data: {
    recognizing: false,
    invoice: null,          // OCR 结果
    title: '',              // 报销事由
    subjectIndex: 0,         // 推荐科目
    subjects: dict.subjects,
    remark: ''
  },
  // 模拟拍照识别
  doRecognize() {
    this.setData({ recognizing: true });
    wx.showLoading({ title: 'AI 识别中...' });
    setTimeout(() => {
      const inv = ocr.recognize();
      const subIdx = dict.subjects.findIndex(s => s.code === inv.suggestedSubject);
      this.setData({
        recognizing: false,
        invoice: inv,
        subjectIndex: subIdx >= 0 ? subIdx : 0
      });
      wx.hideLoading();
      wx.showToast({ title: '识别完成', icon: 'success' });
    }, 1200);
  },
  onTitleInput(e) { this.setData({ title: e.detail.value }); },
  onRemarkInput(e) { this.setData({ remark: e.detail.value }); },
  onSubjectChange(e) { this.setData({ subjectIndex: Number(e.detail.value) }); },
  submit() {
    if (!this.data.invoice) {
      wx.showToast({ title: '请先拍票识别', icon: 'none' }); return;
    }
    if (!this.data.title.trim()) {
      wx.showToast({ title: '请填报销事由', icon: 'none' }); return;
    }
    const bill = store.createBill({
      title: this.data.title,
      amount: this.data.invoice.amount,
      subject: dict.subjects[this.data.subjectIndex].code,
      invoice: this.data.invoice,
      applicant: '李小禾',
      remark: this.data.remark
    });
    wx.showModal({
      title: '提交成功',
      content: `单号 ${bill.id}\n已进入主管审批流程`,
      showCancel: false,
      success: () => {
        this.setData({ invoice: null, title: '', remark: '' });
        wx.switchTab({ url: '/pages/bills/bills' });
      }
    });
  }
});
