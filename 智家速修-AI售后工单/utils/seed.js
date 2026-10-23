// utils/seed.js — 演示工单数据
module.exports = {
  orders: [
    { id: 'GD-20261020-001', device: '智能门锁 Pro', fault: '指纹识别无响应',
      status: 'done', engineer: '李工', createdAt: '2026-10-20 09:12',
      timeline: [
        { t: '10-20 09:12', who: '张住户', action: '提交报修' },
        { t: '10-20 09:13', who: 'AI系统', action: '意图识别完成，初诊：排线松动' },
        { t: '10-20 09:15', who: 'AI系统', action: '远程指导成功，工单闭环' }
      ]},
    { id: 'GD-20261021-002', device: '扫地机器人 X1', fault: '回充失败',
      status: 'dispatched', engineer: '王工', createdAt: '2026-10-21 14:30',
      timeline: [
        { t: '10-21 14:30', who: '张住户', action: '提交报修' },
        { t: '10-21 14:31', who: 'AI系统', action: '初诊：充电触点氧化，需上门' },
        { t: '10-21 14:32', who: 'AI系统', action: '已派单给王工，预计 16:00 上门' }
      ]},
    { id: 'GD-20261022-003', device: '智能窗帘', fault: '电机异响',
      status: 'repairing', engineer: '赵工', createdAt: '2026-10-22 10:05',
      timeline: [
        { t: '10-22 10:05', who: '张住户', action: '提交报修' },
        { t: '10-22 10:06', who: 'AI系统', action: '初诊：齿轮磨损，需上门换电机' },
        { t: '10-22 10:20', who: '赵工', action: '已上门，维修中' }
      ]}
  ]
};
