// utils/dict.js
module.exports = {
  roles: {
    user:     { name: '张住户', label: '报修用户' },
    engineer: { name: '李工',   label: '维修工程师' },
    support:  { name: '客服小周', label: '客服回访' }
  },
  // 工单状态：已提交 → AI诊断中 → 待派单 → 工程师上门 → 维修中 → 已完成 / 已升级
  status: {
    submitted:  { label: '已提交',     cls: 'tag-purple' },
    diagnosing: { label: 'AI诊断中',   cls: 'tag-purple' },
    dispatched: { label: '待上门',     cls: 'tag-orange' },
    repairing:  { label: '维修中',     cls: 'tag-orange' },
    done:       { label: '已完成',     cls: 'tag-green' },
    escalated: { label: '已升级二线', cls: 'tag-red' }
  },
  // AI 故障初诊结果池
  faultPool: [
    { device: '智能门锁 Pro', fault: '指纹识别无响应', cause: '指纹模块排线松动', solution: '重新插拔排线即可', remoteFix: true },
    { device: '扫地机器人 X1', fault: '回充失败', cause: '充电触点氧化', solution: '酒精擦拭触点', remoteFix: true },
    { device: '智能摄像头 360', fault: '画面离线', cause: 'WiFi 模块掉线', solution: '重启路由器+摄像头', remoteFix: true },
    { device: '空调伴侣', fault: '无法遥控', cause: '红外发射器损坏', solution: '需上门更换发射器', remoteFix: false },
    { device: '智能窗帘', fault: '电机异响', cause: '齿轮磨损', solution: '需上门更换电机', remoteFix: false }
  ],
  // 工程师池
  engineers: [
    { name: '李工', skill: '门锁/窗帘', distance: '1.2km', load: '空闲' },
    { name: '王工', skill: '摄像头/空调', distance: '2.5km', load: '半忙' },
    { name: '赵工', skill: '扫地机/门锁', distance: '3.8km', load: '空闲' }
  ]
};
