package com.macteval.controller;

import com.macteval.exception.BusinessException;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Retired endpoints must not create or modify check-in records. */
@RestController
@RequestMapping("/attendance")
public class AttendanceController {

    @RequestMapping({"", "/**"})
    public void retired() {
        throw new BusinessException(410, "考勤功能已取消");
    }
}
