package xiaozhi.modules.device.service;

import java.util.Date;
import java.util.List;
import java.util.Locale;

import org.apache.commons.lang3.StringUtils;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;

import lombok.extern.slf4j.Slf4j;
import xiaozhi.common.exception.RenException;
import xiaozhi.common.redis.RedisKeys;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.modules.agent.dao.AgentDao;
import xiaozhi.modules.agent.entity.AgentEntity;
import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.dto.DeviceReportReqDTO;
import xiaozhi.modules.device.entity.DeviceEntity;

@Slf4j
@Service
public class DeviceAutoBindService {
    private final DeviceDao devices;
    private final AgentDao agents;
    private final DeviceAttributeService attributes;
    private final RedisUtils redis;
    private final TransactionTemplate transactions;
    private final String agentName;

    public DeviceAutoBindService(DeviceDao devices, AgentDao agents, DeviceAttributeService attributes,
            RedisUtils redis, PlatformTransactionManager transactionManager,
            @Value("${KSZ_OTA_AUTO_BIND_AGENT_NAME:}") String agentName) {
        this.devices = devices;
        this.agents = agents;
        this.attributes = attributes;
        this.redis = redis;
        this.transactions = new TransactionTemplate(transactionManager);
        this.agentName = agentName.trim();
    }

    public void bindIfAbsent(String deviceId, DeviceReportReqDTO request) {
        if (StringUtils.isBlank(agentName)) {
            return;
        }
        if (deviceId == null) {
            throw new RenException("Invalid device ID");
        }
        String mac = deviceId.toLowerCase(Locale.ROOT);
        if (findDevice(mac) != null) {
            return;
        }
        if (!deviceId.matches("(?i)([0-9a-f]{2}[:-]){5}[0-9a-f]{2}")) {
            throw new RenException("Invalid device ID");
        }
        if (request == null || request.getApplication() == null
                || StringUtils.isBlank(request.getApplication().getVersion())) {
            throw new RenException("首次自动绑定需要 application.version");
        }
        if (StringUtils.isNotBlank(request.getDeviceId()) && !deviceId.equalsIgnoreCase(request.getDeviceId())) {
            throw new RenException("deviceId 与 Device-Id 请求头不一致");
        }
        List<AgentEntity> matches = agents.selectList(new QueryWrapper<AgentEntity>()
                .select("id", "user_id", "agent_name").eq("agent_name", agentName).last("LIMIT 2"));
        if (matches.size() != 1 || matches.getFirst().getUserId() == null
                || !agentName.equals(matches.getFirst().getAgentName())) {
            throw new RenException("临时智能体不存在或名称不唯一，请检查自动绑定配置");
        }
        AgentEntity agent = matches.getFirst();
        Date now = new Date();
        DeviceEntity device = new DeviceEntity();
        device.setId(mac);
        device.setMacAddress(mac);
        device.setAgentId(agent.getId());
        device.setUserId(agent.getUserId());
        device.setCreator(agent.getUserId());
        device.setUpdater(agent.getUserId());
        device.setCreateDate(now);
        device.setUpdateDate(now);
        device.setLastConnectedAt(now);
        device.setAppVersion(request.getApplication().getVersion());
        device.setBoard(request.getBoard() == null ? null : request.getBoard().getType());
        device.setAutoUpdate(0);
        boolean created;
        try {
            created = Boolean.TRUE.equals(transactions.execute(status -> {
                if (findDevice(mac) != null) {
                    return false;
                }
                devices.insert(device);
                attributes.updateAgentName(mac, agent.getAgentName());
                return true;
            }));
        } catch (DuplicateKeyException e) {
            if (findDevice(mac) == null) {
                throw e;
            }
            return;
        }
        if (created) {
            redis.delete(RedisKeys.getAgentDeviceCountById(agent.getId()));
            log.info("设备首次 OTA 自动绑定: deviceId={}, agentId={}", mac, agent.getId());
        }
    }

    private DeviceEntity findDevice(String mac) {
        return devices.selectOne(new QueryWrapper<DeviceEntity>().select("id").eq("mac_address", mac));
    }
}
