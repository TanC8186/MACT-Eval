package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.WindowFeaturesDTO;
import com.macteval.service.WindowAlignmentService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/window-features")
@RequiredArgsConstructor
public class WindowFeaturesController {

    private final WindowAlignmentService windowAlignmentService;

    @GetMapping("/{lessonSessionId}")
    @PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<WindowFeaturesDTO>> getByLesson(
            @PathVariable String lessonSessionId,
            Authentication auth) {
        return ApiResponse.ok(windowAlignmentService.getByLesson(lessonSessionId, auth.getName()));
    }
}
