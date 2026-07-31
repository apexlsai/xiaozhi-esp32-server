package xiaozhi.modules.device.service.impl;

import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import org.apache.commons.lang3.StringUtils;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;

import lombok.AllArgsConstructor;
import xiaozhi.common.exception.ErrorCode;
import xiaozhi.common.exception.RenException;
import xiaozhi.common.service.impl.BaseServiceImpl;
import xiaozhi.modules.agent.dao.AgentDao;
import xiaozhi.modules.agent.entity.AgentEntity;
import xiaozhi.modules.device.dao.DeviceAttributeDao;
import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.entity.DeviceAttributeEntity;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAttributeService;

@Service
@AllArgsConstructor
public class DeviceAttributeServiceImpl extends BaseServiceImpl<DeviceAttributeDao, DeviceAttributeEntity>
        implements DeviceAttributeService {

    private final DeviceAttributeDao deviceAttributeDao;
    private final DeviceDao deviceDao;
    private final AgentDao agentDao;

    private static final List<String> SUPPORTED_LANGUAGES = Arrays.asList("en", "zh-cn");

    @Override
    public DeviceAttributeEntity getByDeviceId(String deviceId) {
        if (StringUtils.isBlank(deviceId)) {
            return null;
        }
        QueryWrapper<DeviceAttributeEntity> wrapper = new QueryWrapper<>();
        wrapper.eq("device_id", deviceId);
        return deviceAttributeDao.selectOne(wrapper);
    }

    @Override
    public Map<String, String> getAttributesByDeviceId(String deviceId) {
        DeviceAttributeEntity entity = getByDeviceId(deviceId);
        if (entity == null) {
            return Collections.emptyMap();
        }
        Map<String, String> result = new HashMap<>();
        if (entity.getLanguage() != null) {
            result.put("language", entity.getLanguage());
        }
        if (entity.getLastBeaconId() != null) {
            result.put("last_beacon_id", entity.getLastBeaconId());
        }
        if (entity.getAgentName() != null) {
            result.put("agent_name", entity.getAgentName());
        }
        return result;
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void updateLanguage(String deviceId, String language) {
        if (StringUtils.isBlank(deviceId)) {
            return;
        }
        if (StringUtils.isNotBlank(language) && !SUPPORTED_LANGUAGES.contains(language.toLowerCase())) {
            throw new RenException(ErrorCode.DEVICE_ATTRIBUTE_LANGUAGE_INVALID);
        }
        DeviceAttributeEntity entity = getByDeviceId(deviceId);
        if (entity == null) {
            entity = new DeviceAttributeEntity();
            entity.setDeviceId(deviceId);
            entity.setLanguage(language);
            entity.setAgentName(resolveAgentName(deviceId));
            deviceAttributeDao.insert(entity);
        } else {
            entity.setLanguage(language);
            deviceAttributeDao.updateById(entity);
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void updateLastBeaconId(String deviceId, String lastBeaconId) {
        if (StringUtils.isBlank(deviceId)) {
            return;
        }
        DeviceAttributeEntity entity = getByDeviceId(deviceId);
        if (entity == null) {
            entity = new DeviceAttributeEntity();
            entity.setDeviceId(deviceId);
            entity.setLastBeaconId(lastBeaconId);
            entity.setAgentName(resolveAgentName(deviceId));
            deviceAttributeDao.insert(entity);
        } else {
            entity.setLastBeaconId(lastBeaconId);
            deviceAttributeDao.updateById(entity);
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void updateAgentName(String deviceId, String agentName) {
        if (StringUtils.isBlank(deviceId)) {
            return;
        }
        DeviceAttributeEntity entity = getByDeviceId(deviceId);
        if (entity == null) {
            entity = new DeviceAttributeEntity();
            entity.setDeviceId(deviceId);
            entity.setAgentName(agentName);
            deviceAttributeDao.insert(entity);
        } else {
            entity.setAgentName(agentName);
            deviceAttributeDao.updateById(entity);
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void syncAgentNameByAgentId(String agentId, String agentName) {
        if (StringUtils.isBlank(agentId)) {
            return;
        }
        QueryWrapper<DeviceEntity> deviceWrapper = new QueryWrapper<>();
        deviceWrapper.eq("agent_id", agentId);
        List<DeviceEntity> devices = deviceDao.selectList(deviceWrapper);
        if (devices == null || devices.isEmpty()) {
            return;
        }
        List<String> deviceIds = devices.stream()
                .map(DeviceEntity::getMacAddress)
                .filter(StringUtils::isNotBlank)
                .collect(Collectors.toList());
        for (String deviceId : deviceIds) {
            updateAgentName(deviceId, agentName);
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void saveOrUpdateAttribute(String deviceId, String attrKey, String attrValue) {
        if (StringUtils.isBlank(deviceId) || StringUtils.isBlank(attrKey)) {
            return;
        }
        if ("language".equals(attrKey)) {
            updateLanguage(deviceId, attrValue);
        } else if ("last_beacon_id".equals(attrKey)) {
            updateLastBeaconId(deviceId, attrValue);
        } else if ("agent_name".equals(attrKey)) {
            updateAgentName(deviceId, attrValue);
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void deleteByDeviceId(String deviceId) {
        if (StringUtils.isBlank(deviceId)) {
            return;
        }
        UpdateWrapper<DeviceAttributeEntity> wrapper = new UpdateWrapper<>();
        wrapper.eq("device_id", deviceId);
        deviceAttributeDao.delete(wrapper);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void deleteByDeviceIds(List<String> deviceIds) {
        if (deviceIds == null || deviceIds.isEmpty()) {
            return;
        }
        UpdateWrapper<DeviceAttributeEntity> wrapper = new UpdateWrapper<>();
        wrapper.in("device_id", deviceIds);
        deviceAttributeDao.delete(wrapper);
    }

    private String resolveAgentName(String deviceId) {
        QueryWrapper<DeviceEntity> wrapper = new QueryWrapper<>();
        wrapper.eq("mac_address", deviceId);
        DeviceEntity device = deviceDao.selectOne(wrapper);
        if (device == null || StringUtils.isBlank(device.getAgentId())) {
            return null;
        }
        AgentEntity agent = agentDao.selectById(device.getAgentId());
        return agent == null ? null : agent.getAgentName();
    }
}
