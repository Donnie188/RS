// utils/dict.js — 角色 / 状态 / 科目字典
module.exports = {
  roles: {
    employee: { name: '李小禾', empNo: 'LI-2024', dept: '市场部', label: '普通员工' },
    manager:  { name: '王建国', empNo: 'M-0801', dept: '市场部', label: '部门主管' },
    finance:  { name: '陈雅琴', empNo: 'CW-0312', dept: '财务部', label: '财务审核' }
  },
  // 单据状态机：draft → submitted → reviewing(主管) → auditing(财务) → paid / rejected
  status: {
    draft:      { label: '草稿',     cls: 'tag-gray' },
    submitted:  { label: '已提交',   cls: 'tag-blue' },
    reviewing:  { label: '主管审批中', cls: 'tag-orange' },
    auditing:   { label: '财务复核中', cls: 'tag-blue' },
    paid:       { label: '已打款',   cls: 'tag-green' },
    rejected:   { label: '已驳回',   cls: 'tag-red' }
  },
  // 推荐报销科目
  subjects: [
    { code: 'TRV-01', name: '差旅费-交通',   rule: '凭票实报，市内地铁/打车均可' },
    { code: 'TRV-02', name: '差旅费-住宿',   rule: '一线城市 ≤ 450/晚，其他 ≤ 350/晚' },
    { code: 'ENT-01', name: '业务招待费',     rule: '需注明招待对象，单次 ≤ 500' },
    { code: 'OFF-01', name: '办公费',         rule: '凭发票实报，单件 ≤ 2000' },
    { code: 'MKT-01', name: '市场推广费',     rule: '需附活动方案与预算编号' }
  ],
  // 发票类型
  invoiceTypes: ['增值税专用发票', '增值税普通发票', '电子发票', '出租车票', '机票行程单', '火车票']
};
