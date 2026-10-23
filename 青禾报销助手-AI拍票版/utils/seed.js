// utils/seed.js — 演示数据：3 张示例发票 + 4 张历史单据
module.exports = {
  // 模拟 OCR 识别结果池（拍票后随机返回一条）
  invoicePool: [
    { type: '增值税普通发票', seller: '星巴克咖啡（陆家嘴店）', amount: 48.00,  date: '2026-10-20', taxNo: '0431****8821', subject: 'OFF-01' },
    { type: '出租车票',       seller: '上海强生出租',             amount: 86.50,  date: '2026-10-21', taxNo: '沪B-DJ221', subject: 'TRV-01' },
    { type: '增值税专用发票', seller: '汉庭酒店（南京东路店）',   amount: 396.00, date: '2026-10-19', taxNo: '0311****5566', subject: 'TRV-02' },
    { type: '电子发票',       seller: '美团外卖',                 amount: 128.00, date: '2026-10-22', taxNo: 'EL-20261022-091', subject: 'ENT-01' }
  ],
  // 历史单据
  bills: [
    { id: 'BX-20261018-001', title: '国庆出差-北京客户拜访', amount: 1286.50, status: 'paid',     subject: 'TRV-01', applicant: '李小禾', createdAt: '2026-10-18', timeline: [
      { t: '2026-10-18 09:12', who: '李小禾', action: '提交报销单' },
      { t: '2026-10-18 14:30', who: '王建国', action: '主管审批通过' },
      { t: '2026-10-19 10:05', who: '陈雅琴', action: '财务复核通过，已打款' }
    ]},
    { id: 'BX-20261020-002', title: '市场部团建聚餐',       amount: 862.00,  status: 'reviewing', subject: 'ENT-01', applicant: '李小禾', createdAt: '2026-10-20', timeline: [
      { t: '2026-10-20 18:40', who: '李小禾', action: '提交报销单' },
      { t: '2026-10-21 09:00', who: '王建国', action: '待审批' }
    ]},
    { id: 'BX-20261015-003', title: '办公室文具采购',       amount: 234.80,  status: 'rejected',  subject: 'OFF-01', applicant: '李小禾', createdAt: '2026-10-15', timeline: [
      { t: '2026-10-15 11:20', who: '李小禾', action: '提交报销单' },
      { t: '2026-10-16 16:45', who: '陈雅琴', action: '驳回：发票抬头缺失公司税号' }
    ]},
    { id: 'BX-20261022-004', title: '客户来访-商务招待',     amount: 512.00,  status: 'submitted', subject: 'ENT-01', applicant: '李小禾', createdAt: '2026-10-22', timeline: [
      { t: '2026-10-22 20:15', who: '李小禾', action: '提交报销单' }
    ]}
  ]
};
