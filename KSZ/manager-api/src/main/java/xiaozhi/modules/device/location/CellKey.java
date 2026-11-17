package xiaozhi.modules.device.location;

import java.util.Set;

import xiaozhi.modules.device.dto.DeviceCellInfo;

public record CellKey(String value, int providerMnc, long lac, long cellid) {
    public static CellKey from(DeviceCellInfo cell) {
        if (cell == null || !Integer.valueOf(1).equals(cell.getValid())
                || cell.getStat() == null || !Set.of(1, 5).contains(cell.getStat())
                || !"460".equals(cell.getMcc()) || cell.getMnc() == null
                || cell.getLac() == null || cell.getLac() < 0 || cell.getLac() > 65535
                || cell.getCellid() == null || cell.getCellid() <= 0 || cell.getCellid() > 268435455
                || cell.getAct() == null || !Set.of(0, 2, 7).contains(cell.getAct())) {
            return null;
        }
        int network;
        try {
            network = Integer.parseInt(cell.getMnc());
        } catch (NumberFormatException error) {
            return null;
        }
        int providerMnc;
        if (Set.of(0, 2, 4, 7, 8).contains(network)) {
            providerMnc = 0;
        } else if (Set.of(1, 6, 9).contains(network)) {
            providerMnc = 1;
        } else {
            return null;
        }
        return new CellKey("460:" + network + ":" + cell.getAct() + ":" + cell.getLac()
                + ":" + cell.getCellid(), providerMnc, cell.getLac(), cell.getCellid());
    }
}
