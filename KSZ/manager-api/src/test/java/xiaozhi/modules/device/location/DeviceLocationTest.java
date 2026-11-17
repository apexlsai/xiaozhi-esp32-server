package xiaozhi.modules.device.location;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.io.IOException;
import java.util.UUID;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

import xiaozhi.modules.device.dto.DeviceCellInfo;
import xiaozhi.modules.device.dto.DeviceCellReport;
import xiaozhi.modules.device.service.DeviceCellService;

class DeviceLocationTest {
    private static final String MAC = "80:f1:b2:d4:ce:64";
    private static final CellGeo GEO = new CellGeo("深圳市", "广东省深圳市宝安区沙井街道咸田路",
            22.737858, 113.824730, 118);
    private CellLocationStore store;
    private DeviceCellService reports;
    private TanshuCellClient client;
    private DeviceLocationService service;
    private JdbcTemplate jdbc;

    @BeforeEach
    void setup() throws Exception {
        DriverManagerDataSource source = new DriverManagerDataSource(
                "jdbc:h2:mem:" + UUID.randomUUID() + ";MODE=MySQL;DB_CLOSE_DELAY=-1", "sa", "");
        new ResourceDatabasePopulator(new ClassPathResource("db/changelog/ksz-cell-location.sql")).execute(source);
        jdbc = new JdbcTemplate(source);
        store = new CellLocationStore(jdbc);
        reports = mock(DeviceCellService.class);
        client = mock(TanshuCellClient.class);
        when(client.lookup(any(), anyString())).thenReturn(GEO);
        service = service(true);
    }

    DeviceLocationService service(boolean enabled) {
        return new DeviceLocationService(store, reports,
                new CellLocationSettings(enabled, "test-key", 2592000, 60, 86400, ""), client);
    }

    DeviceCellReport report(String mac, long cellid, int valid, long time) {
        DeviceCellInfo cell = new DeviceCellInfo();
        cell.setValid(valid);
        cell.setStat(1);
        cell.setMcc("460");
        cell.setMnc("00");
        cell.setAct(7);
        cell.setLac(10158L);
        cell.setCellid(cellid);
        return new DeviceCellReport(mac, "ksz-traveler-esp32p4", time, cell);
    }

    DeviceCellReport report(String mac, long cellid) {
        return report(mac, cellid, 1, System.currentTimeMillis());
    }

    @Test
    void persistsAndSharesTowerAcrossDevicesAndServiceRestart() throws Exception {
        store.record(report(MAC, 88090510));
        DeviceGeo first = service.resolve(MAC);
        assertEquals("cell", first.source());
        assertEquals("深圳市", first.city());
        assertEquals(118, first.position().radius());
        store.record(report("80:f1:b2:d4:ce:65", 88090510));
        DeviceLocationService restarted = service(true);
        assertEquals("深圳市", restarted.resolve(MAC).city());
        assertEquals("深圳市", restarted.resolve("80:f1:b2:d4:ce:65").city());
        verify(client, times(1)).lookup(any(), anyString());
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM ksz_cell_location_cache", Integer.class));
        assertEquals(2, jdbc.queryForObject("SELECT COUNT(*) FROM ksz_device_location", Integer.class));
    }

    @Test
    void changedTowerAndExpiredCacheTriggerNewLookup() throws Exception {
        store.record(report(MAC, 88090510));
        service.resolve(MAC);
        jdbc.update("UPDATE ksz_cell_location_cache SET expires_at=0");
        service.resolve(MAC);
        store.record(report(MAC, 88090511));
        service.resolve(MAC);
        verify(client, times(3)).lookup(any(), anyString());
    }

    @Test
    void failureHasDatabaseBackoffAndUsesConfiguredCity() throws Exception {
        store.record(report(MAC, 88090510));
        store.configure(MAC, "cell", "深圳");
        when(client.lookup(any(), anyString())).thenThrow(new IOException("provider_failed"));
        DeviceGeo first = service.resolve(MAC);
        assertEquals("default", first.source());
        assertEquals("深圳", first.city());
        assertNull(first.position());
        assertTrue(first.retryAt() > System.currentTimeMillis());
        assertEquals("retry_pending", service(true).resolve(MAC).status());
        verify(client, times(1)).lookup(any(), anyString());
    }

    @Test
    void fixedAndDisabledModesDoNotCallProvider() throws Exception {
        store.record(report(MAC, 88090510));
        store.configure(MAC, "fixed", "深圳");
        assertEquals("fixed", service.resolve(MAC).source());
        store.configure(MAC, "disabled", "深圳");
        assertEquals("default", service.resolve(MAC).source());
        store.configure(MAC, "cell", null);
        assertEquals("unknown", service(false).resolve(MAC).source());
        verifyNoInteractions(client);
    }

    @Test
    void invalidAndStaleReportsDoNotReuseEarlierLocation() throws Exception {
        long now = System.currentTimeMillis();
        store.record(report(MAC, 88090510, 1, now - 1000));
        service.resolve(MAC);
        store.record(report(MAC, 88090510, 0, now));
        assertEquals("unknown", service.resolve(MAC).source());
        store.record(report(MAC, 88090510, 1, now - 500));
        assertEquals(0, store.device(MAC).report().cell().getValid());
        String staleMac = "80:f1:b2:d4:ce:66";
        store.record(report(staleMac, 88090510, 1, now - 86401000));
        assertEquals("report_expired", service.resolve(staleMac).status());
        verify(client, times(1)).lookup(any(), anyString());
    }

    @Test
    void missingReportRetainsWifiIpAndImportsPreexistingRedisCell() {
        assertEquals("ip", service.resolve(MAC).source());
        when(reports.get(MAC)).thenReturn(report(MAC, 88090510));
        assertEquals("cell", service.resolve(MAC).source());
        assertNotNull(store.device(MAC).report());
    }

    @Test
    void concurrentClaimPreventsDuplicateApiAndExpiredLeaseCanRecover() {
        assertTrue(store.claim("tower", 1000));
        assertFalse(store.claim("tower", 1001));
        assertTrue(store.claim("tower", 16000));
    }

    @Test
    void providerParsesActualExampleAndRejectsBusinessErrorsAndBadCoordinates() throws Exception {
        String json = """
                {"code":1,"data":{"lat":"22.737858","lng":"113.824730","radius":118,
                "addr":"广东省深圳市宝安区沙井街道咸田路"}}
                """;
        assertEquals(GEO, TanshuCellClient.parse(json));
        assertThrows(IOException.class, () -> TanshuCellClient.parse("{\"code\":10001}"));
        assertThrows(IOException.class, () -> TanshuCellClient.parse(json.replace("22.737858", "NaN")));
        assertThrows(IOException.class, () -> TanshuCellClient.parse(json.replace("113.824730", "181")));
    }

    @Test
    void towerChangeDuringLookupDoesNotReturnObsoletePosition() throws Exception {
        store.record(report(MAC, 88090510));
        when(client.lookup(any(), anyString())).thenAnswer(invocation -> {
            store.record(report(MAC, 88090511));
            return GEO;
        });
        DeviceGeo result = service.resolve(MAC);
        assertEquals("cell_changed", result.status());
        assertNull(result.position());
    }

    @Test
    void providerNetworkMappingPreservesCarrierAndRejectsInvalidRegistration() {
        DeviceCellInfo info = report(MAC, 88090510).cell();
        assertEquals(0, CellKey.from(info).providerMnc());
        info.setMnc("01");
        assertEquals(1, CellKey.from(info).providerMnc());
        info.setMnc("03");
        assertNull(CellKey.from(info));
        info.setMnc("00");
        info.setStat(0);
        assertNull(CellKey.from(info));
    }
}
