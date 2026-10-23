// utils/ocr.js — 模拟发票 OCR 识别
// 真实环境这里调用微信 OCR 插件或云函数；演示版从 seed 池随机取一条
const seed = require('./seed.js');

module.exports = {
  // 模拟拍照识别：返回一张结构化发票
  recognize() {
    const pool = seed.invoicePool;
    const pick = pool[Math.floor(Math.random() * pool.length)];
    return {
      ...pick,
      confidence: (0.92 + Math.random() * 0.06).toFixed(2),
      // 模拟 AI 推荐科目
      suggestedSubject: pick.subject,
      // 模拟合规校验
      check: this.checkInvoice(pick)
    };
  },
  checkInvoice(inv) {
    const issues = [];
    if (inv.amount > 500 && inv.subject === 'ENT-01') {
      issues.push('招待费超 500 元阈值，需主管额外说明');
    }
    if (inv.type === '增值税专用发票' && inv.amount < 100) {
      issues.push('专票金额过小，建议索取普票');
    }
    return { pass: issues.length === 0, issues };
  }
};
