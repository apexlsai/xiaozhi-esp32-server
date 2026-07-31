package xiaozhi.modules.device.service;

import java.util.List;
import java.util.Map;

import xiaozhi.common.service.BaseService;
import xiaozhi.modules.device.entity.DeviceAttributeEntity;

public interface DeviceAttributeService extends BaseService<DeviceAttributeEntity> {

    /**
     * 获取设备扩展属性实体
     * 
     * @param deviceId 设备ID
     * @return 设备属性实体，不存在则返回 null
     */
    DeviceAttributeEntity getByDeviceId(String deviceId);

    /**
     * 获取设备所有扩展属性（Map 形式，兼容旧接口）
     * 
     * @param deviceId 设备ID
     * @return key-value 属性映射
     */
    Map<String, String> getAttributesByDeviceId(String deviceId);

    /**
     * 更新设备语言
     * 
     * @param deviceId 设备ID
     * @param language 语言代码（en, zh-cn）
     */
    void updateLanguage(String deviceId, String language);

    /**
     * 更新设备蓝牙信标ID
     * 
     * @param deviceId     设备ID
     * @param lastBeaconId 蓝牙信标ID
     */
    void updateLastBeaconId(String deviceId, String lastBeaconId);

    /**
     * 更新设备关联智能体名称
     *
     * @param deviceId  设备ID
     * @param agentName 智能体名称
     */
    void updateAgentName(String deviceId, String agentName);

    /**
     * 按智能体ID批量同步设备属性中的智能体名称
     *
     * @param agentId   智能体ID
     * @param agentName 智能体名称
     */
    void syncAgentNameByAgentId(String agentId, String agentName);

    /**
     * 保存或更新设备属性（兼容旧接口）
     * 
     * @param deviceId  设备ID
     * @param attrKey   属性key（language 或 last_beacon_id）
     * @param attrValue 属性值
     */
    void saveOrUpdateAttribute(String deviceId, String attrKey, String attrValue);

    /**
     * 删除设备所有属性
     * 
     * @param deviceId 设备ID
     */
    void deleteByDeviceId(String deviceId);

    /**
     * 批量删除设备属性
     * 
     * @param deviceIds 设备ID列表
     */
    void deleteByDeviceIds(List<String> deviceIds);
}
