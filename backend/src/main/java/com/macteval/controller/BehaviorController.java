package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.BehaviorDTO;
import com.macteval.service.BehaviorService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/behavior")
@RequiredArgsConstructor
public class BehaviorController {

    private final BehaviorService behaviorService;

    @GetMapping("/{lessonSessionId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<BehaviorDTO>>> getByLesson(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        return ApiResponse.ok(behaviorService.getByLesson(lessonSessionId, auth.getName()));
    }
}
