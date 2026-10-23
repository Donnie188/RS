// utils/ai.js — 模拟 AI 诊断与派单
const dict = require('./dict.js');

module.exports = {
  // AI 故障初诊：随机返回一个诊断结果
  diagnose(device, faultDesc) {
    const pool = dict.faultPool;
    const pick = pool[Math.floor(Math.random() * pool.length)];
    return {
      device: pick.device,
      fault: faultDesc || pick.fault,
      cause: pick.cause,
      solution: pick.solution,
      remoteFix: pick.remoteFix,
      confidence: (0.88 + Math.random() * 0.1).toFixed(2)
    };
  },
  // AI 智能派单：根据故障匹配最近工程师
  dispatch(diagnosis) {
    const pool = dict.engineers;
    // 简单按距离排序取第一个
    const sorted = [...pool].sort((a,b) => parseFloat(a.distance) - parseFloat(b.distance));
    return sorted[0];
  }
};
