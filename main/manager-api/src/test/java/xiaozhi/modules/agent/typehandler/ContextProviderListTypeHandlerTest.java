package xiaozhi.modules.agent.typehandler;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import org.apache.ibatis.type.TypeHandler;
import org.apache.ibatis.type.TypeHandlerRegistry;
import org.junit.jupiter.api.Test;

import xiaozhi.modules.agent.dto.ContextProviderDTO;

class ContextProviderListTypeHandlerTest {

    private final ContextProviderListTypeHandler handler = new ContextProviderListTypeHandler();

    @Test
    void parseKeepsContextProviderDtoElementType() {
        List<ContextProviderDTO> providers = handler
                .parse("[{\"url\":\"https://example.com/context\",\"headers\":{\"Authorization\":\"Bearer token\"}}]");

        assertEquals(1, providers.size());
        assertInstanceOf(ContextProviderDTO.class, providers.get(0));
        assertEquals("https://example.com/context", providers.get(0).getUrl());
        assertEquals("Bearer token", providers.get(0).getHeaders().get("Authorization"));
    }

    @Test
    void parseBlankJsonAsEmptyList() {
        assertTrue(handler.parse(" ").isEmpty());
    }

    @Test
    void myBatisCanInstantiateHandlerForListField() {
        TypeHandler<?> typeHandler = new TypeHandlerRegistry().getInstance(List.class,
                ContextProviderListTypeHandler.class);

        assertInstanceOf(ContextProviderListTypeHandler.class, typeHandler);
    }
}
