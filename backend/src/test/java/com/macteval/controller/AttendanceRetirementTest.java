package com.macteval.controller;

import com.macteval.config.GlobalExceptionHandler;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class AttendanceRetirementTest {
    private final MockMvc mvc = MockMvcBuilders.standaloneSetup(new AttendanceController())
            .setControllerAdvice(new GlobalExceptionHandler()).build();

    @Test
    void readsAndMutationsReturnGoneWithoutAService() throws Exception {
        for (String path : new String[]{"/attendance", "/attendance/lesson-1", "/attendance/lesson-1/init", "/attendance/lesson-1/confirm"}) {
            mvc.perform(get(path)).andExpect(status().isGone())
                    .andExpect(jsonPath("code").value(410))
                    .andExpect(jsonPath("message").value("考勤功能已取消"));
            mvc.perform(post(path).contentType("application/json").content("{}"))
                    .andExpect(status().isGone());
        }
    }
}
